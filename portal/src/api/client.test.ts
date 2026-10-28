import { afterEach, describe, expect, it, vi } from "vitest";

import { mockApi } from "../test/fetch";
import { ApiError, get, onSignedOut, post, query } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
  document.cookie = "portalcsrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});

describe("api client", () => {
  it("sends the CSRF token from the cookie the page names, and a fresh idempotency key per write", async () => {
    document.body.innerHTML = '<div id="root" data-csrf-cookie="portalcsrf"></div>';
    document.cookie = "portalcsrf=tok%2Den";
    const calls = mockApi({ "POST /things": { status: 201, json: { ok: true } }, "GET /things": { json: [] } });

    await post("/things", { a: 1 });
    await post("/things", { a: 1 });
    await get("/things");

    expect(calls[0]!.headers["X-CSRFToken"]).toBe("tok-en");
    expect(calls[0]!.body).toEqual({ a: 1 });
    expect(calls[0]!.headers["Idempotency-Key"]).not.toBe(calls[1]!.headers["Idempotency-Key"]);
    expect(calls[2]!.headers["X-CSRFToken"]).toBeUndefined();
  });

  it("turns problem responses into ApiError and reports a lost session", async () => {
    mockApi({
      "GET /orders": { status: 401, json: { title: "Unauthorized", detail: "Sign in." } },
      "POST /auth/session": { status: 401, json: { title: "Unauthorized", detail: "Wrong email or password." } },
    });
    const signedOut = vi.fn();
    const stop = onSignedOut(signedOut);

    await expect(get("/orders")).rejects.toMatchObject({ status: 401, detail: "Sign in." });
    expect(signedOut).toHaveBeenCalledTimes(1);
    // A failed sign-in is not a lost session.
    await expect(post("/auth/session", {})).rejects.toBeInstanceOf(ApiError);
    expect(signedOut).toHaveBeenCalledTimes(1);
    stop();
  });

  it("builds query strings without empty values", () => {
    expect(query({ a: 1, b: "", c: null, d: undefined, e: true })).toBe("?a=1&e=true");
    expect(query({})).toBe("");
  });
});
