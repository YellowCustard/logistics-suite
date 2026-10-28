import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { App, queryClient } from "./App";
import { mockApi } from "./test/fetch";

const buyer = { user_id: "u1", email: "buyer@sdb.example", roles: ["client_user"], all_sites: false, site_ids: [] };
const page = {
  items: [
    {
      id: "s1",
      number: "SHP-000002",
      order_number: "SO-000002",
      client_ref: "PO-77",
      ship_to_name: "Spar Midrand",
      required_by: "2026-10-30",
      status: "in_transit",
    },
  ],
  next_cursor: null,
};

afterEach(() => {
  vi.unstubAllGlobals();
  queryClient.clear();
});

it("signs a client in to the portal and shows their shipments, newest first", async () => {
  let signedIn = false;
  const calls = mockApi({
    "GET /auth/csrf": { status: 204 },
    "GET /auth/session": () => (signedIn ? { json: buyer } : { status: 401, json: { title: "Unauthorized" } }),
    "POST /auth/session": () => {
      signedIn = true;
      return { json: buyer };
    },
    "GET /shipments": { json: page },
  });
  render(<App />);

  await userEvent.type(await screen.findByLabelText("Email"), " buyer@sdb.example ");
  await userEvent.type(screen.getByLabelText("Password"), "pw-123456789");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

  expect(await screen.findByRole("link", { name: "SHP-000002" })).toHaveAttribute("href", "/portal/shipments/s1");
  expect(screen.getByText("buyer@sdb.example")).toBeInTheDocument();
  const signIn = calls.find((c) => c.method === "POST" && c.path === "/auth/session");
  expect(signIn?.body).toEqual({ email: "buyer@sdb.example", password: "pw-123456789", app: "portal" });
  expect(calls.find((c) => c.path.startsWith("/shipments?"))?.path).toContain("newest_first=true");
});

it("shows why the portal refused a sign-in", async () => {
  mockApi({
    "GET /auth/csrf": { status: 204 },
    "GET /auth/session": { status: 401, json: { title: "Unauthorized" } },
    "POST /auth/session": {
      status: 403,
      json: { title: "Forbidden", detail: "The portal is for client users; staff use the scanner and the admin." },
    },
  });
  render(<App />);

  await userEvent.type(await screen.findByLabelText("Email"), "op@example.com");
  await userEvent.type(screen.getByLabelText("Password"), "pw-123456789");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

  expect(await screen.findByText(/portal is for client users/)).toBeInTheDocument();
});

it("treats an existing staff session as signed out", async () => {
  mockApi({
    "GET /auth/csrf": { status: 204 },
    "GET /auth/session": { json: { ...buyer, roles: ["operator"] } },
  });
  render(<App />);
  expect(await screen.findByRole("heading", { name: "Sign in to the Nuronteq portal" })).toBeInTheDocument();
});
