"""
Topic guardrails: the assistant answers only from the documents in the
knowledge base. Deterministic layers, cheapest first -- none of them spends a
model call:

  1. screen_query()      blocks jailbreak / prompt-extraction attempts and
                         answers bare greetings, before any credit or LLM use
  2. sanitize_history()  drops forged or poisoned prior turns the client sends
  3. (prompt)            SCOPE_POLICY in rag_core/local_chat.py
  4. evidence gate       retrieval/chat_service.py refuses any answer produced
                         without actually reading document content
  5. leaks_internals()   catches answers that echo internal instructions

Patterns are deliberately phrase-level so ordinary document questions such as
"which clause lets us ignore late fees?" are never blocked.
"""

import re
import unicodedata

REFUSAL_OFF_TOPIC = (
    "I can only answer questions about the documents in this knowledge base. "
    "Try asking about something in your files, for example a summary, a specific figure, "
    "or where a topic is mentioned."
)
REFUSAL_BLOCKED = (
    "I can't help with that request. I can only answer questions about the documents in this knowledge base."
)
GREETING_REPLY = "Hi! I can answer questions about the documents in this knowledge base. What would you like to know?"
THANKS_REPLY = "You're welcome! Ask me anything about the documents in this knowledge base."
GENERIC_ERROR = "Something went wrong while answering. Please try again."
# The search agent used all its steps without reaching an answer. Usually the information is not in the
# documents, or the question is very broad; saying so is more useful than a generic error.
SEARCH_LIMIT_REPLY = (
    "I searched the documents but could not find a clear answer within the search limit. The information may "
    "not be in the documents you have access to, or the question may be too broad. Try naming a document, "
    "a project or an identifier."
)

# Tools that return real document content. browse_documents/get_document only return
# listings and metadata, so they don't count as evidence.
EVIDENCE_TOOLS = frozenset({"get_page_content", "get_document_structure"})
# Listings/metadata. On their own they only ground an answer that actually names one of the
# documents they returned ("what documents do we have?"), never free-form text.
METADATA_TOOLS = frozenset({"browse_documents", "get_document"})

MAX_HISTORY_MESSAGE_CHARS = 2000

_ZERO_WIDTH = re.compile("[​-‏‪-‮⁠﻿]")

# Deliberately excludes "ok"/"yes"/"no": those are replies to a clarifying question, not greetings.
_GREETING = re.compile(r"(hi|hii+|hello|hey|hola|good (morning|afternoon|evening))( there| team)?[ !.,]*")
_THANKS = re.compile(r"(thanks|thank you|thx|cheers)( a lot| so much| very much)?[ !.,]*")

_ROLE_WORDS = r"(instructions?|prompts?|programming|directives?|guidelines you|rules you|restrictions?|guardrails?|system message)"
_BLOCK_PATTERNS = [
    re.compile(p)
    for p in (
        # override attempts
        rf"\b(ignore|disregard|forget|override|bypass|skip)\b[^.?!\n]{{0,40}}\b(previous|prior|above|earlier|all|your|these|those|any)\b[^.?!\n]{{0,25}}\b{_ROLE_WORDS}",
        r"\b(ignore|disregard|forget) (everything|all) (above|before|you (were|have been) told)",
        r"\bnew instructions?\s*:",
        # persona / jailbreak
        r"\byou are now\b",
        r"\bfrom now on,? you (are|will|must|shall)\b",
        r"\bpretend (to be|you are|you're|that you)\b",
        r"\b(role-?play|act|behave) as (an? )?(dan|unrestricted|unfiltered|jailbroken|evil|developer|hacker|different)",
        r"\b(dan|jailbreak|jailbroken|developer|god|sudo|admin|debug|unrestricted) mode\b",
        r"\bdo anything now\b",
        # prompt / configuration extraction
        r"\b(reveal|show|print|repeat|display|output|leak|dump|expose|recite|tell me|give me)\b[^.?!\n]{0,30}\b(your|the|ur)\b[^.?!\n]{0,20}\b(system prompt|system message|initial prompt|hidden prompt|pre-?prompt|developer message)",
        r"\b(reveal|show|print|repeat|display|output|leak|dump|expose|recite|tell me|give me|what (is|are))\b[^.?!\n]{0,25}\byour (instructions|rules|guidelines|programming|configuration|initial prompt)",
        r"\bsystem prompt\b",
        r"\b(browse_documents|search_content|get_page_content|get_document_structure|get_document|remove_document)\b",
        # fake role / chat-template markers
        r"<\|im_(start|end)\|>",
        r"\[/?inst\]",
        r"<</?sys>>",
        r"(^|\n)\s*(#{1,3}\s*)?(system|assistant|developer)\s*:",
    )
]

_LEAK_PATTERNS = [
    re.compile(p)
    for p in (
        r"\b(browse_documents|search_content|get_page_content|get_document_structure|get_document|remove_document)\b",
        r"document-focused assistant",
        r"never narrate your own process",
        r"my (system )?(prompt|instructions) (say|says|state|states|tell|tells)",
    )
]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    return text.lower()


def screen_query(query: str) -> tuple[str, str | None]:
    """Returns (verdict, canned_reply). verdict is 'ok', 'greeting' or 'blocked'."""
    lowered = normalize(query)
    for pattern in _BLOCK_PATTERNS:
        if pattern.search(lowered):
            return "blocked", REFUSAL_BLOCKED
    collapsed = " ".join(lowered.split())
    if _GREETING.fullmatch(collapsed):
        return "greeting", GREETING_REPLY
    if _THANKS.fullmatch(collapsed):
        return "greeting", THANKS_REPLY
    return "ok", None


def sanitize_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """The client sends prior turns back to us, so they are untrusted: cap their
    size and drop any that look like injection attempts."""
    clean: list[dict[str, str]] = []
    for message in history:
        content = message["content"][:MAX_HISTORY_MESSAGE_CHARS]
        if screen_query(content)[0] == "blocked":
            continue
        clean.append({"role": message["role"], "content": content})
    return clean


def leaks_internals(answer: str) -> bool:
    lowered = normalize(answer)
    return any(pattern.search(lowered) for pattern in _LEAK_PATTERNS)
