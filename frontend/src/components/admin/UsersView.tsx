"use client";

import { useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import clsx from "clsx";
import { Loader2, Minus, Pencil, Plus, RotateCcw, ShieldCheck, Trash2, UserPlus, X } from "lucide-react";
import { createUser, deleteUser, fetchUsers, resetUserUsage, updateUser } from "@/lib/api";
import type { ManagedUser, Role } from "@/lib/types";

interface Props {
  currentUserId: string;
  onUnauthorized: () => void;
  /** Changing your own password revokes your session, so the app signs you out. */
  onSelfPasswordChanged: () => void;
}

type DialogState = { mode: "create" } | { mode: "edit"; user: ManagedUser } | null;

function relativeMinutes(seconds: number): string {
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h ${Math.ceil((seconds % 3600) / 60)}m`;
  return `${Math.max(1, Math.ceil(seconds / 60))} min`;
}

function StatusPill({ user }: { user: ManagedUser }) {
  if (!user.is_active) return <Pill tone="muted">Disabled</Pill>;
  if (user.role === "admin") return <Pill tone="ok">Active</Pill>;
  if (user.quota.blocked_until) return <Pill tone="warn">Blocked · {relativeMinutes(user.quota.retry_after_seconds)}</Pill>;
  return <Pill tone="ok">Active</Pill>;
}

function Pill({ tone, children }: { tone: "ok" | "warn" | "muted"; children: React.ReactNode }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium",
        tone === "ok" && "bg-surface text-success",
        tone === "warn" && "bg-warning-soft text-warning",
        tone === "muted" && "bg-surface text-muted",
      )}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {children}
    </span>
  );
}

export function UsersView({ currentUserId, onUnauthorized, onSelfPasswordChanged }: Props) {
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogState>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setUsers(await fetchUsers());
      setError(null);
    } catch (err) {
      if (err instanceof Error && err.message === "unauthorized") onUnauthorized();
      else setError(err instanceof Error ? err.message : "Failed to load users");
    } finally {
      setLoading(false);
    }
  }, [onUnauthorized]);

  useEffect(() => {
    void load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, [load]);

  const costHint = users.find((u) => u.role === "user")?.quota.cost ?? 10;

  const run = async (userId: string, action: () => Promise<unknown>) => {
    setBusyId(userId);
    try {
      await action();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background">
      <div className="shrink-0 border-b border-border bg-surface-raised px-4 py-6 sm:px-6">
        <div className="mx-auto flex max-w-6xl flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-accent">Administration</div>
            <h1 className="font-display text-3xl font-medium tracking-[-0.03em]">Users</h1>
            <p className="mt-1 text-sm text-muted">
              Create accounts and set how many messages each person can send. Each message costs {costHint} credits.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setDialog({ mode: "create" })}
            className="inline-flex w-fit items-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-accent-fg transition hover:-translate-y-0.5 hover:shadow-md"
          >
            <UserPlus className="h-4 w-4" strokeWidth={1.75} />
            New user
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
        <div className="mx-auto max-w-6xl">
          {error && (
            <p role="alert" className="mb-3 rounded-lg border border-border bg-surface px-3 py-2 text-xs text-danger">
              {error}
            </p>
          )}
          {loading ? (
            <div className="flex justify-center py-16 text-muted">
              <Loader2 className="h-5 w-5 animate-spin" />
            </div>
          ) : (
            <div className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-surface-raised shadow-soft">
              <div className="hidden grid-cols-[minmax(0,1.4fr)_minmax(0,1.2fr)_minmax(0,0.7fr)_minmax(0,0.9fr)_auto] gap-4 bg-surface px-4 py-2.5 text-[11px] font-medium uppercase tracking-wide text-muted md:grid">
                <span>User</span>
                <span>Credits</span>
                <span>Messages left</span>
                <span>Status</span>
                <span className="w-[108px] text-right">Actions</span>
              </div>
              {users.map((user) => {
                const isSelf = user.user_id === currentUserId;
                const metered = user.role === "user";
                const pct = user.credit_limit > 0 ? Math.min(100, (user.quota.used / user.credit_limit) * 100) : 0;
                return (
                  <div
                    key={user.user_id}
                    className="grid gap-x-4 gap-y-2 px-4 py-3.5 md:grid-cols-[minmax(0,1.4fr)_minmax(0,1.2fr)_minmax(0,0.7fr)_minmax(0,0.9fr)_auto] md:items-center"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium">{user.username}</span>
                        {user.role === "admin" && (
                          <span className="inline-flex items-center gap-1 rounded-md bg-surface px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
                            <ShieldCheck className="h-3 w-3" /> Admin
                          </span>
                        )}
                        {isSelf && <span className="text-[10px] text-muted">(you)</span>}
                        {user.managed_by_env && (
                          <span className="rounded-md bg-surface px-1.5 py-0.5 text-[10px] font-medium text-muted" title="Set in the .env file">
                            .env
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 text-[11px] text-muted">
                        {user.last_login_at ? `Last sign-in ${new Date(user.last_login_at).toLocaleString()}` : "Never signed in"}
                      </div>
                    </div>

                    <div className="min-w-0">
                      {metered ? (
                        <>
                          <div className="text-xs tabular-nums text-foreground">
                            {user.quota.used} / {user.credit_limit}
                          </div>
                          <div className="mt-1.5 h-1.5 w-full max-w-[160px] overflow-hidden rounded-full bg-surface">
                            <div className="h-full rounded-full bg-foreground transition-all" style={{ width: `${pct}%` }} />
                          </div>
                        </>
                      ) : (
                        <span className="text-xs text-muted">Unlimited</span>
                      )}
                    </div>

                    <div className="text-sm tabular-nums">{metered ? user.quota.messages_left : "—"}</div>
                    <div>
                      <StatusPill user={user} />
                    </div>

                    <div className="flex items-center justify-end gap-0.5 md:w-[108px]">
                      {busyId === user.user_id ? (
                        <Loader2 className="mr-2 h-4 w-4 animate-spin text-muted" />
                      ) : (
                        <>
                          {metered && user.quota.used > 0 && (
                            <IconButton
                              label="Reset usage"
                              onClick={() => void run(user.user_id, () => resetUserUsage(user.user_id))}
                            >
                              <RotateCcw className="h-3.5 w-3.5" />
                            </IconButton>
                          )}
                          <IconButton label="Edit user" onClick={() => setDialog({ mode: "edit", user })}>
                            <Pencil className="h-3.5 w-3.5" />
                          </IconButton>
                          {!isSelf && !user.managed_by_env && (
                            <IconButton
                              label="Delete user"
                              danger
                              onClick={() => {
                                if (window.confirm(`Delete "${user.username}"? They will be signed out and lose access.`)) {
                                  void run(user.user_id, () => deleteUser(user.user_id));
                                }
                              }}
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </IconButton>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <AnimatePresence>
        {dialog && (
          <UserDialog
            key="user-dialog"
            state={dialog}
            costHint={costHint}
            isSelf={dialog.mode === "edit" && dialog.user.user_id === currentUserId}
            onClose={() => setDialog(null)}
            onSaved={async (changedOwnPassword) => {
              setDialog(null);
              if (changedOwnPassword) {
                onSelfPasswordChanged();
                return;
              }
              await load();
            }}
          />
        )}
      </AnimatePresence>
    </div>
  );
}

function IconButton({
  label,
  onClick,
  danger,
  children,
}: {
  label: string;
  onClick: () => void;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className={clsx(
        "rounded-lg p-2 text-muted transition hover:bg-surface",
        danger ? "hover:text-danger" : "hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function UserDialog({
  state,
  costHint,
  isSelf,
  onClose,
  onSaved,
}: {
  state: NonNullable<DialogState>;
  costHint: number;
  isSelf: boolean;
  onClose: () => void;
  onSaved: (changedOwnPassword: boolean) => void | Promise<void>;
}) {
  const reduced = useReducedMotion();
  const editing = state.mode === "edit" ? state.user : null;
  const locked = Boolean(editing?.managed_by_env);
  const [username, setUsername] = useState(editing?.username ?? "");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>(editing?.role ?? "user");
  const [isActive, setIsActive] = useState(editing?.is_active ?? true);
  const [creditLimit, setCreditLimit] = useState(editing?.credit_limit ?? 100);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.document.addEventListener("keydown", onKey);
    return () => window.document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const step = Math.max(costHint, 1);
  const messages = Math.floor(creditLimit / step);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (!/^[A-Za-z0-9_.-]{3,32}$/.test(username)) {
      setError("Username must be 3–32 characters: letters, numbers, dot, dash or underscore.");
      return;
    }
    if (!editing && password.length < 10) {
      setError("Password must be at least 10 characters.");
      return;
    }
    if (password && password.length < 10) {
      setError("Password must be at least 10 characters.");
      return;
    }
    if (role === "user" && creditLimit < step) {
      setError(`Credit limit must be at least ${step} (one message).`);
      return;
    }
    setBusy(true);
    try {
      if (editing) {
        await updateUser(editing.user_id, {
          ...(!locked && username !== editing.username ? { username } : {}),
          ...(!locked && password ? { password } : {}),
          ...(!isSelf && role !== editing.role ? { role } : {}),
          ...(!isSelf && isActive !== editing.is_active ? { is_active: isActive } : {}),
          ...(creditLimit !== editing.credit_limit ? { credit_limit: creditLimit } : {}),
        });
        await onSaved(isSelf && Boolean(password));
      } else {
        await createUser({ username, password, role, credit_limit: creditLimit });
        await onSaved(false);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  };

  const inputClass =
    "w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none transition placeholder:text-muted focus:border-accent focus:ring-4 focus:ring-accent-soft disabled:opacity-60";

  return (
    <motion.div
      initial={reduced ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.18, ease: "easeOut" }}
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="user-dialog-title"
    >
      <motion.form
        onSubmit={submit}
        initial={reduced ? false : { opacity: 0, y: 12, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 12, scale: 0.98 }}
        transition={{ duration: 0.22, ease: "easeOut" }}
        className="flex max-h-[90vh] w-full max-w-md flex-col overflow-hidden rounded-xl border border-border-strong bg-background shadow-panel"
      >
        <header className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
          <h2 id="user-dialog-title" className="text-base font-semibold tracking-tight">
            {editing ? `Edit ${editing.username}` : "New user"}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-lg p-2 text-muted transition hover:bg-surface hover:text-foreground"
          >
            <X className="h-4 w-4" strokeWidth={1.75} />
          </button>
        </header>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-muted">Username</span>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus={!locked}
              disabled={locked}
              autoComplete="off"
              className={inputClass}
              placeholder="e.g. priya.sharma"
            />
          </label>

          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-muted">
              {editing ? "New password (leave blank to keep the current one)" : "Password"}
            </span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={locked}
              autoComplete="new-password"
              className={inputClass}
              placeholder={locked ? "Set by ADMIN_PASSWORD in .env" : "At least 10 characters"}
            />
            {locked && (
              <span className="mt-1.5 block text-[11px] text-muted">
                This account is managed by the server&apos;s <code>.env</code> file. To change the password, edit
                <code> ADMIN_PASSWORD</code> there and restart the backend.
              </span>
            )}
            {editing && password && (
              <span className="mt-1.5 block text-[11px] text-muted">
                {isSelf ? "You will be signed out after saving." : "Their current sessions will be signed out."}
              </span>
            )}
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-muted">Role</span>
              <select
                value={role}
                onChange={(e) => setRole(e.target.value as Role)}
                disabled={isSelf || locked}
                className={inputClass}
              >
                <option value="user">User (chat only)</option>
                <option value="admin">Admin</option>
              </select>
            </label>
            {editing && (
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-muted">Status</span>
                <select
                  value={isActive ? "active" : "disabled"}
                  onChange={(e) => setIsActive(e.target.value === "active")}
                  disabled={isSelf || locked}
                  className={inputClass}
                >
                  <option value="active">Active</option>
                  <option value="disabled">Disabled</option>
                </select>
              </label>
            )}
          </div>

          {role === "user" ? (
            <div>
              <span className="mb-1.5 block text-xs font-medium text-muted">Credit allowance</span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setCreditLimit((v) => Math.max(step, v - step))}
                  aria-label="Decrease credits"
                  className="rounded-lg border border-border p-2 text-muted transition hover:bg-surface hover:text-foreground"
                >
                  <Minus className="h-4 w-4" />
                </button>
                <input
                  type="number"
                  min={step}
                  step={step}
                  value={creditLimit}
                  onChange={(e) => setCreditLimit(Math.max(0, Math.floor(Number(e.target.value) || 0)))}
                  className={clsx(inputClass, "text-center tabular-nums")}
                />
                <button
                  type="button"
                  onClick={() => setCreditLimit((v) => v + step)}
                  aria-label="Increase credits"
                  className="rounded-lg border border-border p-2 text-muted transition hover:bg-surface hover:text-foreground"
                >
                  <Plus className="h-4 w-4" />
                </button>
              </div>
              <p className="mt-1.5 text-[11px] text-muted">
                = <span className="font-semibold text-foreground">{messages}</span> message{messages === 1 ? "" : "s"} per cycle ({step} credits each).
                When it runs out they wait 1 hour, then it refills.
              </p>
            </div>
          ) : (
            <p className="text-[11px] text-muted">Admins can manage documents and users and are not credit-limited.</p>
          )}

          {error && (
            <p role="alert" className="text-xs text-danger">
              {error}
            </p>
          )}
        </div>

        <footer className="flex shrink-0 justify-end gap-2 border-t border-border px-4 py-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-border px-3.5 py-2 text-sm font-medium text-muted transition hover:bg-surface hover:text-foreground"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy}
            className="inline-flex items-center gap-2 rounded-lg bg-accent px-3.5 py-2 text-sm font-semibold text-accent-fg transition hover:opacity-90 disabled:opacity-60"
          >
            {busy && <Loader2 className="h-4 w-4 animate-spin" />}
            {editing ? "Save changes" : "Create user"}
          </button>
        </footer>
      </motion.form>
    </motion.div>
  );
}
