"use client";

import { useState } from "react";
import { ArrowLeft, ArrowRight, Loader2 } from "lucide-react";
import { login } from "@/lib/api";
import { Logo } from "@/components/Logo";

interface Props {
  onLoggedIn: () => void;
  onBack?: () => void;
}

export function LoginForm({ onLoggedIn, onBack }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const token = await login(username, password);
      window.localStorage.setItem("auth_token", token);
      onLoggedIn();
    } catch (err) {
      setError(
        err instanceof Error && err.message.startsWith("Too many")
          ? err.message
          : "Invalid username or password.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-5 py-16">
      <form onSubmit={submit} className="w-full max-w-[390px] animate-fade-up">
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            className="mb-8 inline-flex items-center gap-2 text-xs font-semibold text-muted transition hover:text-foreground"
          >
            <ArrowLeft className="h-4 w-4" /> Back to overview
          </button>
        )}

        <div className="flex justify-center">
          <Logo showDescriptor />
        </div>

        <h2 className="mt-8 text-center text-2xl font-semibold tracking-[-0.035em] text-foreground">
          Welcome back
        </h2>
        <p className="mt-2 text-center text-sm leading-6 text-muted">
          Sign in with your workspace credentials to continue.
        </p>

        <div className="mt-9 space-y-4">
          <label className="block">
            <span className="mb-2 block text-xs font-semibold text-foreground">Username</span>
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoFocus
              required
              autoComplete="username"
              placeholder="Enter your username"
              className="h-12 w-full rounded-lg border border-border-strong bg-surface-raised px-3.5 text-sm text-foreground outline-none transition placeholder:text-muted focus:border-accent focus:ring-4 focus:ring-accent-soft"
            />
          </label>
          <label className="block">
            <span className="mb-2 block text-xs font-semibold text-foreground">Password</span>
            <input
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              type="password"
              required
              autoComplete="current-password"
              placeholder="Enter your password"
              className="h-12 w-full rounded-lg border border-border-strong bg-surface-raised px-3.5 text-sm text-foreground outline-none transition placeholder:text-muted focus:border-accent focus:ring-4 focus:ring-accent-soft"
            />
          </label>
        </div>

        {error && (
          <p role="alert" className="mt-3 rounded-lg bg-danger-soft px-3 py-2 text-xs text-danger">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          className="mt-7 inline-flex h-12 w-full items-center justify-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-accent-fg transition hover:-translate-y-0.5 hover:shadow-md disabled:translate-y-0 disabled:opacity-50"
        >
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowRight className="h-4 w-4" />}
          {busy ? "Signing in..." : "Continue"}
        </button>
      </form>
    </main>
  );
}
