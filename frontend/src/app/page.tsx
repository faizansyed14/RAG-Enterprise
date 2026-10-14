"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import clsx from "clsx";
import {
  BookOpen,
  Database,
  FileText,
  LogOut,
  Menu,
  MessageSquare,
  Activity,
  ChevronLeft,
  ChevronRight,
  PanelLeft,
  PanelLeftClose,
  ShieldCheck,
  Trash2,
  Users,
  SlidersHorizontal,
} from "lucide-react";
import { deleteFolder, fetchDocuments, fetchFolders, fetchMe } from "@/lib/api";
import { isUnfiledFolder, UNFILED_FOLDER_ID } from "@/lib/folders";
import type { ChatMessage, Citation, DiagramEvidence, DocumentOut, Folder, Me, Quota } from "@/lib/types";
import {
  type ChatSession,
  createSession,
  deleteSession,
  getActiveSessionId,
  loadSessions,
  saveSessions,
  setActiveSessionId,
  setStorageScope,
  upsertSession,
} from "@/lib/chatStore";
import { UsersView } from "@/components/admin/UsersView";
import { ChunkEditorView } from "@/components/admin/ChunkEditorView";
import { SourcesView } from "@/components/admin/SourcesView";
import { HealthView } from "@/components/admin/HealthView";
import { LoginForm } from "@/components/auth/LoginForm";
import { BusinessLanding } from "@/components/marketing/BusinessLanding";
import { ChatWindow } from "@/components/chat/ChatWindow";
import { ChatsHome } from "@/components/chat/ChatsHome";
import type { QuotaState } from "@/components/chat/CreditsMeter";
import { DocumentsView } from "@/components/documents/DocumentsView";
import { Logo } from "@/components/Logo";
import { ThemeToggle } from "@/components/ThemeToggle";
import { PreviewPane, citationToTarget, diagramToTarget, type PreviewTarget } from "@/components/preview/PreviewPane";

type View = "chats" | "chat" | "documents" | "users" | "chunks" | "sources" | "health";

export default function Home() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [authScreen, setAuthScreen] = useState<"landing" | "login">("landing");
  const [documents, setDocuments] = useState<DocumentOut[]>([]);
  const [folders, setFolders] = useState<Folder[]>([]);
  const [activeFolderId, setActiveFolderId] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [previewTarget, setPreviewTarget] = useState<PreviewTarget | null>(null);
  const [view, setView] = useState<View>("chats");
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [quotaState, setQuotaState] = useState<QuotaState | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [bootAttempt, setBootAttempt] = useState(0);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSave = useRef<{
    sessionId: string;
    patch: { messages: ChatMessage[]; selectedDocIds: string[] | null };
  } | null>(null);

  useEffect(() => {
    setAuthed(!!window.localStorage.getItem("auth_token"));
    setSidebarOpen(window.innerWidth >= 768);
  }, []);

  const applyProfile = useCallback((profile: Me) => {
    setMe(profile);
    setQuotaState(profile.quota ? { quota: profile.quota, receivedAt: Date.now() } : null);
  }, []);

  // The server is the source of truth for who you are and what you have left; the
  // token is never decoded client-side. Chat history is scoped to the user id.
  useEffect(() => {
    if (!authed) {
      setMe(null);
      setQuotaState(null);
      setSessions([]);
      setActiveId(null);
      setView("chats");
      return;
    }
    let cancelled = false;
    setBootError(null);
    fetchMe()
      .then((profile) => {
        if (cancelled) return;
        setStorageScope(profile.user_id, profile.role === "admin");
        applyProfile(profile);
        const loaded = loadSessions().map((session) => ({ ...session, selectedDocIds: null }));
        saveSessions(loaded);
        setSessions(loaded);
        const active = getActiveSessionId();
        setActiveId(active && loaded.some((session) => session.id === active) ? active : loaded[0]?.id ?? null);
        setView("chats");
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof Error && err.message === "unauthorized") {
          window.localStorage.removeItem("auth_token");
          setAuthed(false);
        } else {
          setBootError("Could not reach the server.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [authed, applyProfile, bootAttempt]);

  const reload = useCallback(() => {
    fetchDocuments()
      .then(setDocuments)
      .catch((err) => {
        if (err instanceof Error && err.message === "unauthorized") {
          window.localStorage.removeItem("auth_token");
          setAuthed(false);
        }
      });
  }, []);

  const reloadFolders = useCallback(() => {
    fetchFolders()
      .then(setFolders)
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!authed) return;
    reload();
    reloadFolders();
    const interval = setInterval(reload, 3000);
    return () => clearInterval(interval);
  }, [authed, reload, reloadFolders]);

  const handleDeleteFolder = async (folderId: string) => {
    if (isUnfiledFolder(folderId)) return;
    await deleteFolder(folderId);
    if (activeFolderId === folderId) setActiveFolderId(null);
    reloadFolders();
    reload();
  };

  const writeSave = useCallback(
    (sessionId: string, patch: { messages: ChatMessage[]; selectedDocIds: string[] | null }) => {
      setSessions((previous) => {
        const current = previous.find((session) => session.id === sessionId);
        if (!current) return previous;
        return upsertSession({
          ...current,
          messages: patch.messages,
          selectedDocIds: patch.selectedDocIds,
        });
      });
    },
    [],
  );

  const flushPendingSave = useCallback(() => {
    if (saveTimer.current) {
      clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    if (pendingSave.current) {
      writeSave(pendingSave.current.sessionId, pendingSave.current.patch);
      pendingSave.current = null;
    }
  }, [writeSave]);

  const onSessionUpdate = useCallback(
    (sessionId: string, patch: { messages: ChatMessage[]; selectedDocIds: string[] | null }) => {
      pendingSave.current = { sessionId, patch };
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(flushPendingSave, 250);
    },
    [flushPendingSave],
  );

  const signOut = useCallback(() => {
    flushPendingSave();
    window.localStorage.removeItem("auth_token");
    setAuthed(false);
  }, [flushPendingSave]);

  const refreshQuota = useCallback(() => {
    fetchMe()
      .then(applyProfile)
      .catch((err) => {
        if (err instanceof Error && err.message === "unauthorized") signOut();
      });
  }, [applyProfile, signOut]);

  const handleQuota = useCallback((quota: Quota) => setQuotaState({ quota, receivedAt: Date.now() }), []);

  useEffect(() => {
    const flush = () => flushPendingSave();
    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", flush);
    return () => {
      window.removeEventListener("pagehide", flush);
      document.removeEventListener("visibilitychange", flush);
    };
  }, [flushPendingSave]);

  if (authed === null) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <div className="h-6 w-6 animate-pulse rounded-full bg-surface" />
      </div>
    );
  }
  if (!authed) {
    return authScreen === "landing" ? (
      <BusinessLanding onSignIn={() => setAuthScreen("login")} />
    ) : (
      <LoginForm onLoggedIn={() => setAuthed(true)} onBack={() => setAuthScreen("landing")} />
    );
  }

  if (!me) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 bg-background">
        {bootError ? (
          <>
            <p className="text-sm text-muted">{bootError}</p>
            <button
              type="button"
              onClick={() => setBootAttempt((n) => n + 1)}
              className="rounded-xl border border-border px-3.5 py-2 text-sm font-medium transition hover:bg-surface"
            >
              Try again
            </button>
          </>
        ) : (
          <div className="h-6 w-6 animate-pulse rounded-full bg-surface" />
        )}
      </div>
    );
  }

  const isAdmin = me.role === "admin";
  const indexedIds = documents.filter((d) => d.status === "indexed").map((d) => d.document_id);
  const activeSession = sessions.find((s) => s.id === activeId) ?? null;
  const sidebarSessions = sessions.filter((s) => s.messages.length > 0 || s.id === activeId);

  const closeSidebarOnMobile = () => {
    if (window.innerWidth < 768) setSidebarOpen(false);
  };

  const selectAllDocuments = () => {
    setActiveFolderId(null);
  };

  const selectFolder = (id: string) => {
    setActiveFolderId(id);
  };

  const handleCitationClick = (citation: Citation) => {
    let target = citationToTarget(citation);
    if (!target && citation.document) {
      const doc = documents.find(
        (item) => item.filename === citation.document || item.filename.startsWith(citation.document.replace(/\.[^.]+$/, "")),
      );
      if (doc) {
        target = { kind: "pdf", documentId: doc.document_id, page: citation.page || 1, label: citation.document };
      }
    }
    if (target) setPreviewTarget(target);
  };

  const startNewChat = () => {
    flushPendingSave();
    const session = createSession();
    setSessions(upsertSession(session));
    setActiveId(session.id);
    setView("chat");
    setPreviewTarget(null);
    closeSidebarOnMobile();
  };

  const startChatWithFolders = (folderIds: string[]) => {
    flushPendingSave();
    const includeUnfiled = folderIds.includes(UNFILED_FOLDER_ID);
    const realIds = folderIds.filter((id) => !isUnfiledFolder(id));
    const docIds = documents
      .filter((d) => {
        if (d.status !== "indexed") return false;
        if (includeUnfiled && d.folder_ids.length === 0) return true;
        return d.folder_ids.some((fid) => realIds.includes(fid));
      })
      .map((d) => d.document_id);
    const names = [
      ...(includeUnfiled ? ["Unfiled"] : []),
      ...folders.filter((f) => realIds.includes(f.folder_id)).map((f) => f.name),
    ];
    const session: ChatSession = {
      ...createSession(),
      title: names.length ? names.join(", ").slice(0, 60) : "New chat",
      selectedDocIds: docIds.length ? docIds : null,
    };
    setSessions(upsertSession(session));
    setActiveId(session.id);
    setView("chat");
    setPreviewTarget(null);
    closeSidebarOnMobile();
  };

  const openSession = (id: string) => {
    flushPendingSave();
    setActiveId(id);
    setActiveSessionId(id);
    setView("chat");
    closeSidebarOnMobile();
  };

  const removeSession = (id: string, event: React.MouseEvent) => {
    event.stopPropagation();
    const next = deleteSession(id);
    setSessions(next);
    if (activeId === id) {
      setActiveId(next[0]?.id ?? null);
      setView("chats");
    }
  };

  return (
    <main className="flex h-screen w-screen overflow-hidden bg-background text-foreground">
      {sidebarOpen && (
        <button
          type="button"
          className="fixed inset-0 z-30 bg-black/55 backdrop-blur-sm md:hidden"
          aria-label="Close sidebar"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        className={clsx(
          "fixed inset-y-0 left-0 z-40 flex w-[280px] flex-col border-r border-sidebar-border bg-sidebar text-sidebar-fg transition-transform duration-200 md:relative",
          sidebarOpen ? "translate-x-0" : "-translate-x-full md:hidden",
        )}
      >
        <div className="flex h-16 shrink-0 items-center border-b border-sidebar-border px-4">
          <Logo inverse showDescriptor />
          <button
            type="button"
            onClick={() => setSidebarOpen(false)}
            className="ml-auto rounded-lg p-2 text-sidebar-muted transition hover:bg-sidebar-hover hover:text-sidebar-fg md:hidden"
            aria-label="Close sidebar"
          >
            <PanelLeftClose className="h-4 w-4" />
          </button>
        </div>

        <div className="px-4 pb-2 pt-4 text-[9px] font-semibold uppercase tracking-[0.15em] text-sidebar-muted">Workspace</div>
        <nav className="space-y-1 px-2 pb-3">
          <button
            type="button"
            onClick={() => {
              flushPendingSave();
              setView("chats");
              closeSidebarOnMobile();
            }}
            className={clsx(
              "flex w-full items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm font-medium transition",
              view === "chats" || view === "chat"
                ? "bg-sidebar-active text-sidebar-fg"
                : "text-sidebar-muted hover:bg-sidebar-hover hover:text-sidebar-fg",
            )}
          >
            <MessageSquare className="h-4 w-4" strokeWidth={1.75} />
            Chats
          </button>
          <button
            type="button"
            onClick={() => {
              flushPendingSave();
              setView("documents");
              selectAllDocuments();
              closeSidebarOnMobile();
            }}
            className={clsx(
              "flex w-full items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm font-medium transition",
              view === "documents" ? "bg-sidebar-active text-sidebar-fg" : "text-sidebar-muted hover:bg-sidebar-hover hover:text-sidebar-fg",
            )}
          >
            <FileText className="h-4 w-4" strokeWidth={1.75} />
            Documents
            <span className="ml-auto text-xs text-sidebar-muted">{documents.length}</span>
          </button>
          {isAdmin && (
            <button
              type="button"
              onClick={() => {
                flushPendingSave();
                setView("sources");
                closeSidebarOnMobile();
              }}
              className={clsx(
                "flex w-full items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm font-medium transition",
                view === "sources" ? "bg-sidebar-active text-sidebar-fg" : "text-sidebar-muted hover:bg-sidebar-hover hover:text-sidebar-fg",
              )}
            >
              <Database className="h-4 w-4" strokeWidth={1.75} />
              Sources
            </button>
          )}
          {isAdmin && (
            <button
              type="button"
              onClick={() => {
                flushPendingSave();
                setView("chunks");
                closeSidebarOnMobile();
              }}
              className={clsx(
                "flex w-full items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm font-medium transition",
                view === "chunks" ? "bg-sidebar-active text-sidebar-fg" : "text-sidebar-muted hover:bg-sidebar-hover hover:text-sidebar-fg",
              )}
            >
              <SlidersHorizontal className="h-4 w-4" strokeWidth={1.75} />
              Chunks
            </button>
          )}
          {isAdmin && (
            <button
              type="button"
              onClick={() => {
                flushPendingSave();
                setView("health");
                closeSidebarOnMobile();
              }}
              className={clsx(
                "flex w-full items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm font-medium transition",
                view === "health" ? "bg-sidebar-active text-sidebar-fg" : "text-sidebar-muted hover:bg-sidebar-hover hover:text-sidebar-fg",
              )}
            >
              <Activity className="h-4 w-4" strokeWidth={1.75} />
              Health
            </button>
          )}
          {isAdmin && (
            <button
              type="button"
              onClick={() => {
                flushPendingSave();
                setView("users");
                closeSidebarOnMobile();
              }}
              className={clsx(
                "flex w-full items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm font-medium transition",
                view === "users" ? "bg-sidebar-active text-sidebar-fg" : "text-sidebar-muted hover:bg-sidebar-hover hover:text-sidebar-fg",
              )}
            >
              <Users className="h-4 w-4" strokeWidth={1.75} />
              Users
            </button>
          )}
        </nav>

        {/* History in left nav only while inside an active chat */}
        {view === "chat" && (
          <div className="flex min-h-0 flex-1 flex-col border-t border-sidebar-border pt-4">
            <div className="px-4 pb-2 text-[9px] font-semibold uppercase tracking-[0.15em] text-sidebar-muted">Recent conversations</div>
            <div className="flex-1 overflow-y-auto px-2 pb-2">
              {sidebarSessions.length === 0 && (
                <p className="px-2 py-2 text-xs text-sidebar-muted">No chats yet</p>
              )}
              {sidebarSessions.map((session) => (
                <div
                  key={session.id}
                  className={clsx(
                    "group mb-1 flex items-center rounded-lg",
                    session.id === activeId ? "bg-sidebar-active" : "hover:bg-sidebar-hover",
                  )}
                >
                  <button
                    type="button"
                    onClick={() => openSession(session.id)}
                    className="min-w-0 flex-1 truncate px-3 py-2 text-left text-sm text-sidebar-fg"
                    title={session.title}
                  >
                    {session.title}
                  </button>
                  <button
                    type="button"
                    onClick={(e) => removeSession(session.id, e)}
                    className="mr-1 rounded p-1.5 text-sidebar-muted opacity-0 transition hover:text-sidebar-fg group-hover:opacity-100 focus:opacity-100"
                    aria-label="Delete chat"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="mt-auto border-t border-sidebar-border p-3">
          <div className="flex items-center gap-2 rounded-lg bg-sidebar-hover px-3 py-2.5 text-[11px] text-sidebar-muted">
            <span className="relative flex h-2 w-2"><span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent-muted opacity-30" /><span className="relative h-2 w-2 rounded-full bg-accent-muted" /></span>
            <span><strong className="font-semibold text-sidebar-fg">{indexedIds.length}</strong> sources ready</span>
          </div>
        </div>
        <button
          type="button"
          onClick={() => setSidebarOpen(false)}
          className="absolute -right-3.5 top-1/2 z-10 hidden h-14 w-3.5 -translate-y-1/2 items-center justify-center rounded-r-lg border border-l-0 border-sidebar-border bg-sidebar text-sidebar-muted transition hover:w-5 hover:text-sidebar-fg md:flex"
          aria-label="Collapse sidebar"
          title="Collapse sidebar"
        >
          <ChevronLeft className="h-3.5 w-3.5" />
        </button>
      </aside>

      <div className="relative flex min-w-0 flex-1 flex-col overflow-hidden bg-background">
        {!sidebarOpen && (
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            className="absolute left-0 top-1/2 z-20 hidden h-14 w-3.5 -translate-y-1/2 items-center justify-center rounded-r-lg border border-l-0 border-border bg-surface-raised text-muted shadow-soft transition hover:w-5 hover:text-foreground md:flex"
            aria-label="Expand sidebar"
            title="Expand sidebar"
          >
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        )}
        <header className="flex h-16 shrink-0 items-center gap-3 border-b border-border bg-surface-raised px-3 sm:px-5">
          {!sidebarOpen && (
            <button
              type="button"
              onClick={() => setSidebarOpen(true)}
              className="rounded-lg p-2 text-muted transition hover:bg-surface hover:text-foreground"
              aria-label="Open sidebar"
            >
              <PanelLeft className="h-4 w-4" />
            </button>
          )}
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            className={clsx("rounded-lg p-2 text-muted transition hover:bg-surface hover:text-foreground md:hidden", sidebarOpen && "hidden")}
            aria-label="Menu"
          >
            <Menu className="h-5 w-5" />
          </button>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-semibold tracking-[-0.01em]">
              {view === "chats"
                ? "Chats"
                : view === "chat"
                  ? activeSession?.title ?? "New chat"
                  : view === "users"
                    ? "Users"
                    : view === "chunks"
                      ? "Chunk editor"
                      : view === "sources"
                        ? "Sources"
                        : view === "health"
                          ? "System health"
                          : "Document library"}
            </div>
            <div className="mt-0.5 hidden text-[10px] text-muted sm:block">
              {view === "chats"
                ? "Start or continue a conversation"
                : view === "chat"
                  ? "Evidence-backed workspace"
                  : view === "users"
                    ? "Accounts and usage limits"
                    : view === "chunks"
                      ? "Fix, add and switch off chunks; test a search"
                      : view === "sources"
                        ? "Connect folders and shares, watch indexing, set who can read what"
                        : view === "health"
                          ? "Is the search index complete? What is the worker doing?"
                          : isAdmin
                      ? "Manage sources and indexing"
                      : "Browse indexed sources"}
            </div>
          </div>
          <div className="ml-auto flex items-center gap-1">
            {(view === "chats" || view === "chat") && (
              <a
                href="/RAG.html"
                target="_blank"
                rel="noopener noreferrer"
                className="mr-1 inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium text-muted transition hover:border-foreground hover:text-foreground"
                title="How RAG works"
              >
                <BookOpen className="h-3.5 w-3.5" /> <span className="hidden sm:inline">Know more</span>
              </a>
            )}
            <span className="mr-2 hidden items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-[10px] font-medium text-muted sm:inline-flex">
              <ShieldCheck className="h-3 w-3 text-accent" /> Private workspace
            </span>
            <span className="mr-1 hidden max-w-[140px] truncate text-xs font-medium text-muted md:inline" title={me.username}>
              {me.username}
            </span>
            <ThemeToggle />
            <button
              type="button"
              onClick={signOut}
              className="rounded-lg p-2 text-muted transition hover:bg-surface hover:text-foreground"
              aria-label="Sign out"
              title="Sign out"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        </header>

        <div className="min-h-0 flex-1">
          {isAdmin && view === "users" && (
            <UsersView currentUserId={me.user_id} onUnauthorized={signOut} onSelfPasswordChanged={signOut} />
          )}
          {isAdmin && view === "sources" && <SourcesView onUnauthorized={signOut} />}
          {isAdmin && view === "health" && <HealthView onUnauthorized={signOut} />}
          {isAdmin && view === "chunks" && <ChunkEditorView documents={documents} onUnauthorized={signOut} />}
          {view === "documents" && (
            <DocumentsView
              documents={documents}
              folders={folders}
              activeFolderId={activeFolderId}
              onIndexed={reload}
              onFoldersChanged={reloadFolders}
              onOpen={(id) => {
                const doc = documents.find((d) => d.document_id === id);
                setPreviewTarget({ kind: "pdf", documentId: id, page: 1, label: doc?.filename ?? "Document" });
              }}
              onSelectFolder={selectFolder}
              onSelectAll={selectAllDocuments}
              onDeleteFolder={handleDeleteFolder}
              onChatWithFolders={startChatWithFolders}
              readOnly={!isAdmin}
            />
          )}
          {view === "chats" && (
            <ChatsHome
              sessions={sessions}
              onStartNew={startNewChat}
              onOpen={openSession}
              onDelete={removeSession}
            />
          )}
          {view === "chat" && activeSession && (
            <ChatWindow
              key={activeSession.id}
              sessionId={activeSession.id}
              initialMessages={activeSession.messages}
              initialSelectedIds={activeSession.selectedDocIds}
              // Do not send every indexed id with each question. null means "search everything this user may
              // read"; the server resolves that through the permission-aware index. A narrowed selection is still sent.
              documentIds={null}
              documents={documents}
              folders={folders}
              onDocumentsChanged={reload}
              onCitationClick={handleCitationClick}
              onDiagramClick={(evidence) => setPreviewTarget(diagramToTarget(evidence))}
              onSessionUpdate={(patch) => onSessionUpdate(activeSession.id, patch)}
              quotaState={quotaState}
              onQuota={handleQuota}
              onQuotaExpired={refreshQuota}
              onUnauthorized={signOut}
              canUpload={isAdmin}
            />
          )}
        </div>
      </div>

      <PreviewPane target={previewTarget} onClose={() => setPreviewTarget(null)} />
    </main>
  );
}
