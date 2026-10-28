// Same-origin JSON client for /api/v1. The browser session authenticates; writes carry the CSRF token and an
// Idempotency-Key so a retried request is applied once.

const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly title: string,
    readonly detail: string,
    readonly errors: { row?: number; field?: string; message: string }[] = [],
  ) {
    super(detail || title);
  }
}

function csrfToken(): string {
  const name = document.getElementById("root")?.dataset.csrfCookie || "csrftoken";
  const row = document.cookie.split("; ").find((c) => c.startsWith(`${name}=`));
  return row ? decodeURIComponent(row.slice(name.length + 1)) : "";
}

type Listener = () => void;
const signedOutListeners = new Set<Listener>();

/** Called when any request comes back 401, so the app can return to the sign-in page. */
export function onSignedOut(listener: Listener): () => void {
  signedOutListeners.add(listener);
  return () => signedOutListeners.delete(listener);
}

export async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") {
    headers["X-CSRFToken"] = csrfToken();
    headers["Idempotency-Key"] = crypto.randomUUID();
  }
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(BASE + path, {
    method,
    credentials: "same-origin",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    if (response.status === 401 && path !== "/auth/session") signedOutListeners.forEach((l) => l());
    throw new ApiError(response.status, data?.title ?? response.statusText, data?.detail ?? "", data?.errors ?? []);
  }
  return data as T;
}

export const get = <T>(path: string) => request<T>("GET", path);
export const post = <T>(path: string, body?: unknown) => request<T>("POST", path, body);
export const del = <T>(path: string) => request<T>("DELETE", path);

export function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const documentUrl = (id: string) => `${BASE}/documents/${encodeURIComponent(id)}/download`;
