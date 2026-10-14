import type { ChatMessage } from "./types";

export interface ChatSession {
  id: string;
  title: string;
  messages: ChatMessage[];
  selectedDocIds: string[] | null;
  updatedAt: number;
  createdAt: number;
}

const LEGACY_SESSIONS_KEY = "rag_chat_sessions";
const LEGACY_ACTIVE_KEY = "rag_active_session";

// Chat history is stored per user so accounts sharing a browser never see each other's chats.
let storageScope = "anon";
const sessionsKey = () => `${LEGACY_SESSIONS_KEY}:${storageScope}`;
const activeKey = () => `${LEGACY_ACTIVE_KEY}:${storageScope}`;

/** Call once after sign-in, before loading sessions. Pre-accounts history (unscoped keys)
 *  is handed to the first admin that signs in -- never to a regular user. */
export function setStorageScope(userId: string, adoptLegacy: boolean): void {
  storageScope = userId;
  if (typeof window === "undefined") return;
  const store = window.localStorage;
  if (adoptLegacy && store.getItem(sessionsKey()) === null && store.getItem(LEGACY_SESSIONS_KEY) !== null) {
    store.setItem(sessionsKey(), store.getItem(LEGACY_SESSIONS_KEY) as string);
    const active = store.getItem(LEGACY_ACTIVE_KEY);
    if (active) store.setItem(activeKey(), active);
    store.removeItem(LEGACY_SESSIONS_KEY);
    store.removeItem(LEGACY_ACTIVE_KEY);
  }
}

function safeParse<T>(raw: string | null, fallback: T): T {
  if (!raw) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export function loadSessions(): ChatSession[] {
  if (typeof window === "undefined") return [];
  const list = safeParse<ChatSession[]>(window.localStorage.getItem(sessionsKey()), []);
  return list.sort((a, b) => b.updatedAt - a.updatedAt);
}

export function saveSessions(sessions: ChatSession[]): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(sessionsKey(), JSON.stringify(sessions.slice(0, 50)));
}

export function getActiveSessionId(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(activeKey());
}

export function setActiveSessionId(id: string | null): void {
  if (typeof window === "undefined") return;
  if (id) window.localStorage.setItem(activeKey(), id);
  else window.localStorage.removeItem(activeKey());
}

export function createSession(): ChatSession {
  const now = Date.now();
  return {
    id: crypto.randomUUID(),
    title: "New chat",
    messages: [],
    selectedDocIds: null,
    createdAt: now,
    updatedAt: now,
  };
}

export function titleFromMessages(messages: ChatMessage[]): string {
  const first = messages.find((m) => m.role === "user");
  if (!first?.content.trim()) return "New chat";
  const t = first.content.trim().replace(/\s+/g, " ");
  return t.length > 48 ? `${t.slice(0, 48)}…` : t;
}

export function upsertSession(session: ChatSession): ChatSession[] {
  const all = loadSessions().filter((s) => s.id !== session.id);
  const next = [{ ...session, updatedAt: Date.now(), title: titleFromMessages(session.messages) }, ...all];
  saveSessions(next);
  setActiveSessionId(session.id);
  return next;
}

export function deleteSession(id: string): ChatSession[] {
  const next = loadSessions().filter((s) => s.id !== id);
  saveSessions(next);
  if (getActiveSessionId() === id) setActiveSessionId(next[0]?.id ?? null);
  return next;
}
