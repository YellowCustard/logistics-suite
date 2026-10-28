import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { mockApi } from "../test/fetch";
import { renderAt } from "../test/render";
import { Home } from "./Home";
import { Stock } from "./Stock";

const row = (over: Record<string, unknown>) => ({
  site_id: "s1",
  site_code: "TEAK",
  client_id: "c1",
  item_id: "i1",
  sku: "WATER",
  description: "Water 500 ml",
  base_uom: "EA",
  lot: "",
  expiry: null,
  status: "available",
  qty_on_hand: "15.000",
  qty_allocated: "6.000",
  qty_available: "9.000",
  ...over,
});
const stock = {
  items: [row({}), row({ item_id: "i2", sku: "=SUM(A1)", lot: "L1", status: "quarantine", qty_allocated: "0.000", qty_available: "4.000", qty_on_hand: "4.000" })],
  truncated: false,
};

afterEach(() => vi.unstubAllGlobals());

it("lists stock and exports it as a safe CSV", async () => {
  const calls = mockApi({ "GET /sites": { json: { items: [], next_cursor: null } }, "GET /inventory/summary": { json: stock } });
  let saved: Blob | undefined;
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: (b: Blob) => ((saved = b), "blob:x"), revokeObjectURL: () => {} }));
  renderAt("/stock", <Stock />, "/stock?q=wat");

  const water = (await screen.findByText("WATER")).closest("tr")!;
  expect(within(water).getAllByRole("cell").map((c) => c.textContent)).toEqual([
    "TEAK", "WATER", "Water 500 ml", "—", "—", "Available", "15", "6", "9",
  ]);
  expect(calls[calls.length - 1]!.path).toBe("/inventory/summary?q=wat");

  await userEvent.click(screen.getByRole("button", { name: "Export CSV" }));
  const text = await saved!.text();
  expect(text.split("\r\n").slice(0, 3)).toEqual([
    "﻿site,sku,description,lot,expiry,status,on_hand,allocated,available,uom",
    "TEAK,WATER,Water 500 ml,,,available,15.000,6.000,9.000,EA",
    "TEAK,'=SUM(A1),Water 500 ml,L1,,quarantine,4.000,0.000,4.000,EA",
  ]);
});

it("summarises orders, deliveries, stock and open cases", async () => {
  const calls = mockApi({
    "GET /orders/status-counts": {
      json: { counts: { accepted: 0, allocated: 3, backordered: 1, picking: 2, packed: 0, dispatched: 4, delivered: 9, cancelled: 1 }, finished_since: "2026-10-21T00:00:00Z" },
    },
    "GET /shipments": (call) =>
      call.path.includes("delivered_on")
        ? { json: { items: [{ id: "s2", number: "SHP-2", client_ref: "PO-2", ship_to_name: "Rosebank", status: "delivered" }], next_cursor: null } }
        : { json: { items: [], next_cursor: null } },
    "GET /inventory/summary": { json: stock },
    "GET /client/exceptions": {
      json: {
        items: [{ id: "x1", kind: "allocation.short", severity: "medium", status: "open", summary: "SO-7: 1 line(s) short of stock", entity_type: "oms.sales_order", entity_id: "o7", created_at: "2026-10-28T09:00:00Z", resolved_at: null }],
        next_cursor: null,
      },
    },
  });
  renderAt("/", <Home />, "/");

  const short = await screen.findByRole("link", { name: /Short of stock/ });
  expect(short).toHaveAttribute("href", "/orders?status=backordered");
  expect(await within(short).findByText("1")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "SHP-2" })).toHaveAttribute("href", "/shipments/s2");
  expect(await screen.findByRole("link", { name: "SO-7: 1 line(s) short of stock" })).toHaveAttribute("href", "/orders/o7");
  expect(screen.getByText("Units available").nextSibling).toHaveTextContent("13");
  expect(screen.getByText("Not available (quarantine, damaged)").nextSibling).toHaveTextContent("4");
  const day = new Intl.DateTimeFormat("en-CA", { timeZone: "Africa/Johannesburg" }).format(new Date());
  expect(calls.some((c) => c.path.includes(`delivered_on=${day}`))).toBe(true);
});
