"""A language model for the optional features (chunk enrichment and query rewriting).

Deliberately small: one method, `complete(system, user) -> str`, behind any OpenAI-compatible endpoint, so the same
code runs against the application's own gateway, a hosted API, or a local server (Ollama, vLLM, llama.cpp, LM Studio)
when the client's documents may not leave the building.

Nothing in ingestion or search REQUIRES this. Callers treat `get_llm()` returning None, or a call raising LlmError, as
"this feature is not available right now" and carry on without it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Protocol, Sequence

from app.scale.settings import ScaleSettings, get_scale_settings

log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    pass


class LlmClient(Protocol):
    name: str

    def complete(self, system: str, user: str, *, max_tokens: int | None = None, temperature: float = 0.0) -> str: ...


class OpenAICompatLlm:
    """POST {base_url}/chat/completions. Retries transient failures with backoff; raises LlmError after that."""

    def __init__(self, base_url: str, api_key: str, model: str, *, timeout_s: float = 120.0, max_tokens: int = 1024,
                 retries: int = 2) -> None:
        if not model:
            raise LlmError("no model name: set SCALE_LLM_MODEL (or use SCALE_LLM_MODE=repo)")
        from openai import OpenAI

        self.name = model
        self._model = model
        self._max_tokens = max_tokens
        self._retries = max(0, retries)
        self._client = OpenAI(base_url=base_url, api_key=api_key or "none", timeout=timeout_s, max_retries=0)

    def complete(self, system: str, user: str, *, max_tokens: int | None = None, temperature: float = 0.0) -> str:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    max_tokens=max_tokens or self._max_tokens, temperature=temperature,
                )
                content = response.choices[0].message.content
                if content is None:
                    raise LlmError("the model returned no content")
                return content
            except LlmError:
                raise
            except Exception as exc:  # noqa: BLE001 -- network / rate limit / server error
                last = exc
                if attempt < self._retries:
                    time.sleep(min(2 ** attempt, 8))
        raise LlmError(f"model request failed after {self._retries + 1} attempts: {last}")


class FakeLlm:
    """For tests and for trying the plumbing without a model. `answer` is either a fixed string, a list of strings
    (used in turn, the last one repeated) or a function (system, user) -> str. Every call is recorded."""

    name = "fake-llm"

    def __init__(self, answer: str | Sequence[str] | Callable[[str, str], str] = "") -> None:
        self._answer = answer
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_tokens: int | None = None, temperature: float = 0.0) -> str:
        self.calls.append((system, user))
        a = self._answer
        if callable(a):
            return a(system, user)
        if isinstance(a, str):
            return a
        i = min(len(self.calls) - 1, len(a) - 1)
        return a[i]


_OVERRIDE: list[LlmClient | None] = []        # tests push a client here


def set_llm_for_tests(client: LlmClient | None) -> None:
    _OVERRIDE.clear()
    if client is not None:
        _OVERRIDE.append(client)


_cache: dict[tuple, LlmClient] = {}


def get_llm(cfg: ScaleSettings | None = None) -> LlmClient | None:
    """The configured model, or None when SCALE_LLM_MODE=off (or the model cannot be built: it is logged, not raised,
    because every caller has a no-model path)."""
    if _OVERRIDE:
        return _OVERRIDE[0]
    cfg = cfg or get_scale_settings()
    mode = (cfg.llm_mode or "off").lower()
    if mode == "off":
        return None
    try:
        if mode == "repo":
            from app.core.config import get_settings

            app = get_settings()
            model = app.rag_index_model
            model = model[len("openrouter/"):] if model.startswith("openrouter/") else model
            key = (mode, app.openrouter_base_url, model)
            if key not in _cache:
                _cache[key] = OpenAICompatLlm(app.openrouter_base_url, app.openrouter_api_key, model,
                                              timeout_s=cfg.llm_timeout_s, max_tokens=cfg.llm_max_tokens, retries=cfg.llm_retries)
            return _cache[key]
        if mode == "openai":
            key = (mode, cfg.llm_base_url, cfg.llm_model)
            if key not in _cache:
                _cache[key] = OpenAICompatLlm(cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model,
                                              timeout_s=cfg.llm_timeout_s, max_tokens=cfg.llm_max_tokens, retries=cfg.llm_retries)
            return _cache[key]
        log.warning("SCALE_LLM_MODE=%r is not one of off / repo / openai; running without a model", mode)
    except Exception:  # noqa: BLE001
        log.warning("could not set up the language model; running without it", exc_info=True)
    return None


# ── reading what a model wrote ──

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def parse_json(text: str) -> Any | None:
    """The first JSON object or array in a model's answer, or None. Tolerates a code fence, text before and after, and
    trailing commas -- what small local models actually produce."""
    if not text:
        return None
    sources = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for source in sources:
        for variant in (source, re.sub(r",\s*([\]}])", r"\1", source)):
            tried = 0
            for m in re.finditer(r"[\[{]", variant):
                tried += 1
                if tried > 40:                       # do not scan a long ramble for a needle
                    break
                try:
                    value, _ = decoder.raw_decode(variant, m.start())
                except ValueError:
                    continue
                if isinstance(value, (dict, list)):
                    return value
    return None


def complete_json(llm: LlmClient, system: str, user: str, *, max_tokens: int | None = None, attempts: int = 2) -> Any | None:
    """Ask for JSON; ask once more (with the failure named) if the answer cannot be read. None when it never can."""
    prompt = user
    for _ in range(max(1, attempts)):
        out = llm.complete(system, prompt, max_tokens=max_tokens)
        parsed = parse_json(out)
        if parsed is not None:
            return parsed
        prompt = user + "\n\nYour previous answer was not valid JSON. Answer again with ONLY the JSON, nothing else."
    return None


def map_parallel(fn: Callable[[Any], Any], items: Sequence[Any], concurrency: int) -> list[Any]:
    """fn over items, in order, with at most `concurrency` calls in flight. An item whose call raises gives None."""
    def safe(item: Any) -> Any:
        try:
            return fn(item)
        except Exception:  # noqa: BLE001 -- one bad chunk must not lose the whole document
            log.warning("model call failed for one item", exc_info=True)
            return None

    if concurrency <= 1 or len(items) <= 1:
        return [safe(i) for i in items]
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(safe, items))
