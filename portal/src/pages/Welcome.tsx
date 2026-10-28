import { useState, type FormEvent } from "react";

import { post } from "../api/client";
import { ErrorBox } from "../components/ui";

/** Reads the one-time token from the link's fragment (#u=…&t=…) and drops it from the address bar, so it is not
 * left in history or a screenshot. The fragment never reaches a server. */
function takeToken(): { uid: string; token: string } | null {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const uid = params.get("u");
  const token = params.get("t");
  if (window.location.hash) window.history.replaceState(null, "", window.location.pathname);
  return uid && token ? { uid, token } : null;
}

/** Where a new portal user (or one whose password was reset) chooses a password from their setup link. */
export function Welcome() {
  const [link] = useState(takeToken);
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const mismatch = again !== "" && again !== password;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!link || mismatch) return;
    setBusy(true);
    setError(null);
    try {
      await post("/auth/password", { uid: link.uid, token: link.token, password });
      setDone(true);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="signin-page">
      <img src="/static/brand/mark-simple.svg" alt="" width={40} height={40} />
      <h1>Choose your password</h1>
      {done ? (
        <div className="flash ok" role="status">
          Your password is set. <a href="/portal/">Sign in</a>
        </div>
      ) : !link ? (
        <div className="flash bad" role="alert">
          This link is incomplete. Open the whole link you were sent, or ask your administrator for a new one.
        </div>
      ) : (
        <>
          <ErrorBox error={error} />
          <form className="signin" onSubmit={submit}>
            <label htmlFor="password">New password (at least 12 characters)</label>
            <input
              id="password"
              type="password"
              autoComplete="new-password"
              required
              minLength={12}
              maxLength={256}
              autoFocus
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <label htmlFor="again">The same password again</label>
            <input
              id="again"
              type="password"
              autoComplete="new-password"
              required
              value={again}
              onChange={(e) => setAgain(e.target.value)}
            />
            {mismatch && <p className="muted">The two passwords differ.</p>}
            <div className="actions">
              <button type="submit" disabled={busy || mismatch}>
                {busy ? "Saving…" : "Set password"}
              </button>
            </div>
          </form>
        </>
      )}
    </main>
  );
}
