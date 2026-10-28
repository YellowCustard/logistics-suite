import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { mockApi } from "../test/fetch";
import { renderAt } from "../test/render";
import { Settings } from "./Settings";
import { ApiCredentials } from "./settings/ApiCredentials";
import { Users } from "./settings/Users";
import { Webhooks } from "./settings/Webhooks";
import { Welcome } from "./Welcome";

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

const user = (over: Record<string, unknown>) => ({
  id: "u2",
  email: "lerato@sdb.example",
  full_name: "Lerato M",
  role: "client_user",
  is_active: true,
  pending: false,
  last_login: null,
  date_joined: "2026-10-01T08:00:00Z",
  ...over,
});

it("tells users without the admin role who manages settings", () => {
  renderAt("/settings", <Settings />, "/settings");
  expect(screen.getByText(/Your account administrator manages users/)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
});

it("adds a user and shows their one-time link, changes roles, and resets passwords", async () => {
  vi.stubGlobal("confirm", () => true);
  const calls = mockApi({
    "GET /client/users": { json: [user({ id: "u1", email: "user@example.com", role: "client_admin" }), user({})] },
    "POST /client/users": (call) => ({
      status: 201,
      json: { ...user({ id: "u3", pending: true }), ...(call.body as object), setup_link: "/portal/welcome#u=abc&t=tok" },
    }),
    "PATCH /client/users/u2": (call) => ({ json: user(call.body as Record<string, unknown>) }),
    "POST /client/users/u2/setup-link": { json: { setup_link: "/portal/welcome#u=def&t=tok2" } },
  });
  renderAt("/settings/users", <Users />, "/settings/users", [], ["client_admin"]);

  const rows = await screen.findAllByRole("row");
  expect(within(rows[1]!).getByText("You")).toBeInTheDocument(); // no actions on yourself
  expect(within(rows[1]!).queryByRole("combobox")).not.toBeInTheDocument();

  await userEvent.type(screen.getByLabelText("Email"), "thabo@sdb.example");
  await userEvent.selectOptions(screen.getByLabelText("Role"), "client_admin");
  await userEvent.click(screen.getByRole("button", { name: "Add user" }));
  expect(await screen.findByLabelText("Setup link")).toHaveValue(`${window.location.origin}/portal/welcome#u=abc&t=tok`);
  expect(calls.find((c) => c.method === "POST")?.body).toEqual({
    email: "thabo@sdb.example",
    full_name: "",
    role: "client_admin",
  });
  await userEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(screen.queryByLabelText("Setup link")).not.toBeInTheDocument();

  await userEvent.selectOptions(screen.getByLabelText("Role of lerato@sdb.example"), "client_admin");
  await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ role: "client_admin" }));
  expect(calls.find((c) => c.method === "PATCH")?.headers["If-Match"]).toBeUndefined();

  await userEvent.click(screen.getByRole("button", { name: "Reset password" }));
  expect(await screen.findByText("Password reset for lerato@sdb.example")).toBeInTheDocument();
  expect(screen.getByLabelText("Setup link")).toHaveValue(`${window.location.origin}/portal/welcome#u=def&t=tok2`);
});

it("creates an API credential and shows its secret once", async () => {
  const created = {
    id: "k1",
    name: "SDB ERP",
    key_id: "lk_123",
    is_active: true,
    last_used_at: null,
    created_at: "2026-10-28T08:00:00Z",
  };
  let listed: unknown[] = [];
  mockApi({
    "GET /client/api-credentials": () => ({ json: listed }),
    "POST /client/api-credentials": () => {
      listed = [created];
      return { status: 201, json: { ...created, secret: "s3cret" } };
    },
  });
  renderAt("/settings/api", <ApiCredentials />, "/settings/api", [], ["client_admin"]);
  expect(await screen.findByText("No credentials yet.")).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText(/^Name/), "SDB ERP");
  await userEvent.click(screen.getByRole("button", { name: "Create" }));
  expect(await screen.findByLabelText("Client secret")).toHaveValue("s3cret");
  expect(screen.getByLabelText("Client ID")).toHaveValue("lk_123");
  expect(await screen.findByRole("button", { name: "Rotate secret" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(screen.queryByText("s3cret")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Client secret")).not.toBeInTheDocument();
});

it("adds a webhook for the chosen events and shows the delivery log", async () => {
  const hook = {
    id: "w1",
    client_id: "c1",
    url: "https://erp.example/hooks",
    event_types: ["oms.sales_order.delivered"],
    description: "",
    is_active: true,
    created_at: "2026-10-28T08:00:00Z",
  };
  const calls = mockApi({
    "GET /webhooks": { json: [hook] },
    "GET /webhooks/events": { json: { event_types: ["oms.sales_order.accepted", "oms.sales_order.delivered"] } },
    "POST /webhooks": () => ({ status: 201, json: { ...hook, id: "w2", secret: "whsec_x" } }),
    "GET /webhooks/w1/deliveries": {
      json: [
        {
          id: "d1",
          event_id: "e1",
          event_type: "platform.webhook.ping",
          status: "dead",
          attempts: 8,
          last_status_code: 500,
          last_error: "Server error",
          created_at: "2026-10-28T08:00:00Z",
          delivered_at: null,
        },
      ],
    },
  });
  renderAt("/settings/webhooks", <Webhooks />, "/settings/webhooks", [], ["client_admin"]);
  const add = await screen.findByRole("button", { name: "Add webhook" });
  expect(add).toBeDisabled(); // no events chosen yet
  await userEvent.clear(screen.getByLabelText("HTTPS URL"));
  await userEvent.type(screen.getByLabelText("HTTPS URL"), "https://erp.example/hooks");
  await userEvent.click(screen.getByLabelText("oms.sales_order.delivered"));
  await userEvent.click(add);
  expect(await screen.findByLabelText("Signing secret")).toHaveValue("whsec_x");
  expect(calls.find((c) => c.method === "POST")?.body).toEqual({
    url: "https://erp.example/hooks",
    description: "",
    event_types: ["oms.sales_order.delivered"],
  });

  await userEvent.click(screen.getByRole("button", { name: "Show deliveries" }));
  expect(await screen.findByText("Gave up")).toBeInTheDocument();
  expect(screen.getByText("500 · Server error")).toBeInTheDocument();
});

it("sets a password from the link's fragment and clears it from the address bar", async () => {
  window.history.replaceState(null, "", "/portal/welcome#u=abc&t=tok");
  const calls = mockApi({ "POST /auth/password": { status: 204 } });
  render(<Welcome />);
  expect(window.location.hash).toBe("");
  await userEvent.type(screen.getByLabelText(/New password/), "Korhaan-veld-2026");
  await userEvent.type(screen.getByLabelText("The same password again"), "Korhaan-veld-2025");
  expect(screen.getByRole("button", { name: "Set password" })).toBeDisabled();
  await userEvent.clear(screen.getByLabelText("The same password again"));
  await userEvent.type(screen.getByLabelText("The same password again"), "Korhaan-veld-2026");
  await userEvent.click(screen.getByRole("button", { name: "Set password" }));
  expect(await screen.findByText(/Your password is set/)).toBeInTheDocument();
  expect(calls[0]?.body).toEqual({ uid: "abc", token: "tok", password: "Korhaan-veld-2026" });
});

it("explains an incomplete link", () => {
  window.history.replaceState(null, "", "/portal/welcome");
  render(<Welcome />);
  expect(screen.getByText(/This link is incomplete/)).toBeInTheDocument();
});
