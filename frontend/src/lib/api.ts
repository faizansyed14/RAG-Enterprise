import type { ChatEvent, DocumentOut, DocumentRawDump, Folder, ManagedUser, Me, ProgressEvent, Role, TreeNode } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem("auth_token");
}

function authHeaders(): HeadersInit {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function login(username: string, password: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (res.status === 429) {
    const body = await res.json().catch(() => null);
    const seconds = Number(body?.detail?.retry_after_seconds ?? res.headers.get("Retry-After") ?? 60);
    const minutes = Math.max(1, Math.ceil(seconds / 60));
    throw new Error(`Too many attempts. Please try again in ${minutes} minute${minutes === 1 ? "" : "s"}.`);
  }
  if (!res.ok) throw new Error("Invalid username or password");
  const data = await res.json();
  return data.token as string;
}

export async function fetchMe(): Promise<Me> {
  const res = await fetch(`${API_BASE}/api/auth/me`, { headers: authHeaders() });
  if (res.status === 401) throw new Error("unauthorized");
  if (!res.ok) throw new Error(`Failed to load profile: ${res.status}`);
  return res.json();
}

async function readError(res: Response, fallback: string): Promise<Error> {
  if (res.status === 401) return new Error("unauthorized");
  try {
    const body = await res.json();
    const detail = body?.detail;
    if (typeof detail === "string") return new Error(detail);
    if (Array.isArray(detail) && detail[0]?.msg) return new Error(String(detail[0].msg));
  } catch {
    // fall through
  }
  return new Error(`${fallback} (${res.status})`);
}

export interface UserPayload {
  username?: string;
  password?: string;
  role?: Role;
  is_active?: boolean;
  credit_limit?: number;
}

export async function fetchUsers(): Promise<ManagedUser[]> {
  const res = await fetch(`${API_BASE}/api/users`, { headers: authHeaders() });
  if (!res.ok) throw await readError(res, "Failed to load users");
  return res.json();
}

export async function createUser(payload: UserPayload & { username: string; password: string }): Promise<ManagedUser> {
  const res = await fetch(`${API_BASE}/api/users`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw await readError(res, "Could not create user");
  return res.json();
}

export async function updateUser(userId: string, payload: UserPayload): Promise<ManagedUser> {
  const res = await fetch(`${API_BASE}/api/users/${userId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw await readError(res, "Could not update user");
  return res.json();
}

export async function resetUserUsage(userId: string): Promise<ManagedUser> {
  const res = await fetch(`${API_BASE}/api/users/${userId}/reset-usage`, { method: "POST", headers: authHeaders() });
  if (!res.ok) throw await readError(res, "Could not reset usage");
  return res.json();
}

export async function deleteUser(userId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/users/${userId}`, { method: "DELETE", headers: authHeaders() });
  if (!res.ok) throw await readError(res, "Could not delete user");
}

export async function fetchDocuments(options?: { folderId?: string | null; unfiled?: boolean }): Promise<DocumentOut[]> {
  const params = new URLSearchParams();
  if (options?.folderId) params.set("folder_id", options.folderId);
  if (options?.unfiled) params.set("unfiled", "true");
  const qs = params.toString();
  const res = await fetch(`${API_BASE}/api/documents${qs ? `?${qs}` : ""}`, { headers: authHeaders() });
  if (res.status === 401) throw new Error("unauthorized");
  if (!res.ok) throw new Error(`Failed to list documents: ${res.status}`);
  return res.json();
}

export async function uploadDocument(file: File, folderId?: string | null): Promise<{ document_id: string; status?: string }> {
  const form = new FormData();
  form.append("file", file);
  if (folderId) form.append("folder_id", folderId);
  const res = await fetch(`${API_BASE}/api/documents`, {
    method: "POST",
    headers: authHeaders(),
    body: form,
  });
  if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
  return res.json();
}

export async function deleteDocument(documentId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/documents/${documentId}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(`Delete failed: ${res.status}`);
}

export async function setDocumentFolders(documentId: string, folderIds: string[]): Promise<DocumentOut> {
  const res = await fetch(`${API_BASE}/api/documents/${documentId}/folders`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ folder_ids: folderIds }),
  });
  if (!res.ok) throw new Error(`Updating folders failed: ${res.status}`);
  return res.json();
}

export async function fetchFolders(): Promise<Folder[]> {
  const res = await fetch(`${API_BASE}/api/folders`, { headers: authHeaders() });
  if (res.status === 401) throw new Error("unauthorized");
  if (!res.ok) throw new Error(`Failed to list folders: ${res.status}`);
  return res.json();
}

export async function createFolder(name: string): Promise<Folder> {
  const res = await fetch(`${API_BASE}/api/folders`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw new Error(`Create folder failed: ${res.status}`);
  return res.json();
}

export async function renameFolder(folderId: string, name: string): Promise<Folder> {
  const res = await fetch(`${API_BASE}/api/folders/${folderId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw new Error(`Rename folder failed: ${res.status}`);
  return res.json();
}

export async function deleteFolder(folderId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/folders/${folderId}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(`Delete folder failed: ${res.status}`);
}

export async function getDocumentTree(documentId: string): Promise<TreeNode[]> {
  const res = await fetch(`${API_BASE}/api/documents/${documentId}/tree`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`Tree lookup failed: ${res.status}`);
  const data = await res.json();
  return data.tree as TreeNode[];
}

/** `url` is a stored copy to show. It is null when the original is not kept (STORE_ORIGINALS=false): `source` then says where the file lives. */
export interface PreviewInfo {
  url: string | null;
  missing?: boolean;     // the stored copy was deleted from storage
  message?: string;
  stored?: boolean;
  name?: string;
  page_count?: number | null;
  source?: { available: boolean; source?: string; kind?: string; path?: string; url?: string | null };
}

export async function getPreviewInfo(documentId: string): Promise<PreviewInfo> {
  const res = await fetch(`${API_BASE}/api/documents/${documentId}/preview`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`Preview lookup failed: ${res.status}`);
  return (await res.json()) as PreviewInfo;
}

/** Thrown when a document has no file to show (deleted from storage, or never kept). The message is safe to show. */
export class PreviewUnavailableError extends Error {}

export async function getPreviewUrl(documentId: string): Promise<string> {
  const data = await getPreviewInfo(documentId);
  if (!data.url) throw new PreviewUnavailableError(data.message ?? "This document has no stored copy. It is kept at its source.");
  return data.url;
}

export async function getPageText(documentId: string, page: number): Promise<{ page: number; page_count: number | null; text: string }> {
  const res = await fetch(`${API_BASE}/api/documents/${documentId}/pages/${page}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`Page lookup failed: ${res.status}`);
  return res.json();
}

export async function getDocumentRaw(documentId: string): Promise<DocumentRawDump> {
  const res = await fetch(`${API_BASE}/api/documents/${documentId}/raw`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`Raw dump failed: ${res.status}`);
  return res.json();
}

export async function getDiagramPreview(documentId: string, pageNumber: number): Promise<string> {
  const res = await fetch(`${API_BASE}/api/documents/${documentId}/diagrams/${pageNumber}`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(`Diagram preview lookup failed: ${res.status}`);
  const data = await res.json();
  return data.url as string;
}

/** fetch-based SSE reader, since EventSource can't carry an Authorization header. */
export async function streamProgress(
  documentId: string,
  onEvent: (event: ProgressEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/documents/${documentId}/progress`, {
    headers: authHeaders(),
    signal,
  });
  if (!res.ok || !res.body) return;
  await readSSE(res.body, (_event, data) => onEvent(JSON.parse(data)));
}

export async function streamChat(
  query: string,
  documentIds: string[] | null,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
  history?: { role: "user" | "assistant"; content: string }[],
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ query, document_ids: documentIds, history: history ?? [] }),
    signal,
  });
  if (res.status === 401) throw new Error("unauthorized");
  if (res.status === 429) {
    const body = await res.json().catch(() => null);
    const detail = body?.detail;
    if (detail?.code === "rate_limited") {
      // Sending too fast (or a message is still being answered) -- not the credit limit.
      onEvent({ type: "throttled", message: detail?.message, retry_after_seconds: detail?.retry_after_seconds });
      return;
    }
    onEvent({ type: "limit", message: detail?.message ?? "You've reached your usage limit.", quota: detail?.quota });
    return;
  }
  if (!res.ok || !res.body) {
    onEvent({ type: "error", message: `Request failed: ${res.status}` });
    return;
  }
  await readSSE(res.body, (_event, data) => onEvent(JSON.parse(data)));
}

async function readSSE(
  body: ReadableStream<Uint8Array>,
  onFrame: (event: string, data: string) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const eventMatch = frame.match(/^event: (.+)$/m);
      const dataMatch = frame.match(/^data: (.+)$/m);
      if (dataMatch) onFrame(eventMatch?.[1] ?? "message", dataMatch[1]);
    }
  }
}
