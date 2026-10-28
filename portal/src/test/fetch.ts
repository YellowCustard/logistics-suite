import { vi } from "vitest";

export type Call = { method: string; path: string; headers: Record<string, string>; body: unknown };
type Route = (call: Call) => { status?: number; json?: unknown } | undefined;

/** Stubs fetch with a router over "METHOD /path" keys (path without /api/v1, query string kept); records calls. */
export function mockApi(routes: Record<string, Route | { status?: number; json?: unknown }>) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = url.replace(/^\/api\/v1/, "");
      const call: Call = {
        method: init.method ?? "GET",
        path,
        headers: (init.headers ?? {}) as Record<string, string>,
        body: init.body ? JSON.parse(String(init.body)) : undefined,
      };
      calls.push(call);
      const key = `${call.method} ${path.split("?")[0]}`;
      const route = routes[key];
      const answer = typeof route === "function" ? route(call) : route;
      if (!answer) return new Response(JSON.stringify({ title: "Not Found", detail: key }), { status: 404 });
      const status = answer.status ?? 200;
      return new Response(answer.json === undefined || status === 204 ? null : JSON.stringify(answer.json), { status });
    }),
  );
  return calls;
}
