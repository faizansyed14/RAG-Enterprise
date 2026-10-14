import { getToken } from "@/lib/api";

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** Admin calls to /api/scale. Throws Error("unauthorized") on 401 so the caller can sign the person out. */
export async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch(`${API_BASE}/api/scale${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(init?.headers ?? {}) },
  });
  if (res.status === 401) throw new Error("unauthorized");
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // keep the generic message
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export interface UploadResult {
  connection_id: string;
  created: boolean;
  saved: { name: string; size: number }[];
  rejected: { name: string; reason: string }[];
  scan_queued: boolean;
}

/** Multipart upload with progress (fetch cannot report upload progress, XMLHttpRequest can). */
export function uploadFiles(form: FormData, onProgress: (fraction: number) => void): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/api/scale/uploads`);
    const token = getToken();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onerror = () => reject(new Error("The upload could not reach the server."));
    xhr.onload = () => {
      if (xhr.status === 401) return reject(new Error("unauthorized"));
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // not JSON
      }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(body as UploadResult);
      const detail = (body as { detail?: unknown } | null)?.detail;
      reject(new Error(typeof detail === "string" ? detail : xhr.status === 413 ? "That upload is larger than the server allows." : `Upload failed (${xhr.status})`));
    };
    xhr.send(form);
  });
}
