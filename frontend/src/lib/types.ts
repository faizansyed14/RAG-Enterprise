export type DocumentStatus = "queued" | "extracting" | "ocr" | "indexing" | "indexed" | "failed";

export interface DocumentOut {
  document_id: string;
  filename: string;
  doc_type: "pdf" | "docx" | "csv" | "xlsx" | "eml" | "txt" | "json" | "xer" | "py" | "md" | "other";
  is_scanned: boolean;
  page_count: number | null;
  status: DocumentStatus;
  status_detail: string | null;
  error: string | null;
  created_at: string | null;
  folder_ids: string[];
}

export interface Folder {
  folder_id: string;
  name: string;
  created_at: string | null;
  document_count: number;
}

export interface TreeNode {
  title: string;
  node_id: string | null;
  page_number: number | null;
  summary?: string | null;
  prefix_summary?: string | null;
  text?: string | null;
  nodes: TreeNode[];
}

/** Full dump from GET /api/documents/{id}/raw — Postgres row + tree store + diagrams. */
export interface DocumentRawDump {
  document: {
    document_id: string;
    filename: string;
    doc_type: string;
    status: string;
    status_detail: string | null;
    error: string | null;
    is_scanned: boolean;
    page_count: number | null;
    content_hash: string;
    storage_key: string;
    preview_storage_key: string | null;
    rag_doc_id: string | null;
    created_at: string | null;
  };
  index: {
    rag_doc_id: string | null;
    meta: Record<string, unknown> | null;
    pages: { page_number?: number; markdown?: string; [k: string]: unknown }[];
    node_chunks?: { title?: string; level?: number; page_number?: number; text?: string }[];
    tree: TreeNode[];
    error?: string;
  };
  diagrams: {
    page_number: number;
    figure_id: string | null;
    caption: string | null;
    description: string | null;
    ocr_text: string | null;
    image_key: string;
    qdrant_point_id: string;
    embedding_model: string | null;
    callouts: unknown;
  }[];
}

export interface ProgressEvent {
  phase: "extracting" | "ocr" | "embedding" | "indexing" | "indexed" | "failed";
  page?: number;
  total?: number;
  caption?: string | null;
  error?: string;
  tree?: TreeNode[];
}

export interface Citation {
  anchor?: string;
  index?: number;
  document: string;
  doc_id: string | null;
  document_id?: string | null; // our own document id, attached server-side for previewing
  page: number;
  block_id?: string;
}

export interface DiagramEvidence {
  document_id: string;
  page_number: number;
  figure_id: string | null;
  caption: string | null;
  description: string | null;
  storage_key: string;
  score: number;
  vision_verdict: string;
}

export type Role = "admin" | "user";

/** Server-computed usage state. Never derived client-side -- see backend core/quota.py. */
export interface Quota {
  limit: number;
  used: number;
  remaining: number;
  cost: number;
  messages_left: number;
  blocked_until: string | null;
  /** seconds until the block ends, measured by the server at `server_time` */
  retry_after_seconds: number;
  /** how long a block lasts once the allowance is spent */
  block_seconds: number;
  server_time: string;
}

export interface Me {
  user_id: string;
  username: string;
  role: Role;
  quota: Quota | null; // null for admins (unmetered)
}

export interface ManagedUser {
  user_id: string;
  username: string;
  role: Role;
  is_active: boolean;
  credit_limit: number;
  quota: Quota;
  created_at: string | null;
  last_login_at: string | null;
  /** the .env admin: username, password, role and status can only change in .env */
  managed_by_env: boolean;
}

export interface ChatEvent {
  type: "thinking" | "answer" | "tool_call" | "tool_result" | "citations" | "done" | "error" | "usage" | "limit" | "throttled";
  quota?: Quota;
  /** for `throttled`: seconds until the next message may be sent */
  retry_after_seconds?: number;
  delta?: string;
  call_id?: string;
  name?: string;
  arguments?: unknown;
  output?: unknown;
  citations?: Citation[];
  diagrams?: DiagramEvidence[];
  resolved_answer?: string;
  message?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** content with "[[1]](#rag-citation-01)" markers, once citations resolve -- rendered as inline pills */
  resolvedContent?: string;
  streaming?: boolean;
  toolActivity?: { callId?: string; name: string; detail: string; done?: boolean }[];
  citations?: Citation[];
  diagrams?: DiagramEvidence[];
}
