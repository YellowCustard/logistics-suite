import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { mockApi } from "../test/fetch";
import { renderAt } from "../test/render";
import { InboundDetail } from "./InboundDetail";
import { NewInbound, sast } from "./NewInbound";

const sites = {
  items: [{ id: "id-TEAK", code: "TEAK", name: "Teakwood", kind: "warehouse", address: "", is_active: true }],
  next_cursor: null,
};
const items = {
  items: [
    { id: "i1", sku: "WATER", description: "Water", base_uom: "EA", lot_tracked: false, expiry_tracked: false, is_active: true },
    { id: "i2", sku: "JUICE", description: "Juice", base_uom: "EA", lot_tracked: true, expiry_tracked: true, is_active: true },
  ],
  next_cursor: null,
};
const line = (over: Record<string, unknown>) => ({
  id: "nl1",
  line_no: 1,
  item_id: "i1",
  sku: "WATER",
  qty: "10.000",
  uom: "CS",
  lot: "",
  expiry: null,
  qty_received: "8.000",
  status: "closed",
  ...over,
});
const notice = {
  id: "n1",
  number: "ASN-2026-000001",
  client_id: "c1",
  site_id: "id-TEAK",
  client_ref: "PO-55",
  kind: "purchase",
  status: "closed",
  can_cancel: false,
  expected_at: "2026-11-02T06:00:00Z",
  notes: "",
  cancelled_reason: "",
  created_at: "2026-10-28T08:00:00Z",
  lines: [line({}), line({ id: "nl2", line_no: 2, sku: "JUICE", qty: "5.000", qty_received: "5.000" })],
};
const receipt = {
  id: "r1",
  number: "RCV-2026-000001",
  status: "completed",
  completed_at: "2026-11-02T07:00:00Z",
  lines: [
    { id: "rl1", inbound_notice_line_id: "nl1", sku: "WATER", qty: "6.000", uom: "CS", lot: "", expiry: null, condition: "good", photos: [] },
    {
      id: "rl2",
      inbound_notice_line_id: "nl1",
      sku: "WATER",
      qty: "2.000",
      uom: "CS",
      lot: "",
      expiry: null,
      condition: "damaged",
      photos: [{ id: "doc9", mime: "image/jpeg" }],
    },
    { id: "rl3", inbound_notice_line_id: null, sku: "SOAP", qty: "1.000", uom: "EA", lot: "", expiry: null, condition: "good", photos: [] },
  ],
};

afterEach(() => vi.unstubAllGlobals());

it("shows expected against received, damage photos and items not on the notice", async () => {
  const calls = mockApi({
    "GET /sites": { json: sites },
    "GET /inbound-notices/n1": { json: notice },
    "GET /receipts": { json: { items: [receipt], next_cursor: null } },
  });
  renderAt("/inbound/:id", <InboundDetail />, "/inbound/n1");

  const water = (await screen.findAllByText("WATER"))[0]!.closest("tr")!;
  expect(within(water).getAllByRole("cell").map((c) => c.textContent)).toEqual([
    "1", "WATER", "—", "—", "10", "8", "-2", "CS",
  ]);
  expect(within(water).getAllByRole("cell")[6]).toHaveClass("warn");
  expect(await screen.findByRole("heading", { name: "Receipt RCV-2026-000001" })).toBeInTheDocument();
  expect(screen.getByText("(not on the notice)").closest("td")).toHaveTextContent("SOAP");
  expect(screen.getByRole("img", { name: "WATER damage photo 1" })).toHaveAttribute("src", "/api/v1/documents/doc9/download");
  expect(calls.find((c) => c.path.startsWith("/receipts?"))!.path).toBe("/receipts?inbound_notice_id=n1");
  expect(screen.queryByRole("button", { name: "Cancel notice" })).not.toBeInTheDocument();
});

it("does not look for receipts before anything arrives, and cancels with a reason", async () => {
  const calls = mockApi({
    "GET /sites": { json: sites },
    "GET /inbound-notices/n1": { json: { ...notice, status: "accepted", can_cancel: true } },
    "POST /inbound-notices/n1/cancel": { json: { ...notice, status: "cancelled", can_cancel: false, cancelled_reason: "Late" } },
  });
  renderAt("/inbound/:id", <InboundDetail />, "/inbound/n1");

  await userEvent.click(await screen.findByRole("button", { name: "Cancel notice" }));
  await userEvent.type(screen.getByLabelText("Reason"), "Late");
  await userEvent.click(screen.getByRole("button", { name: "Cancel notice" }));
  expect(await screen.findByText("Late")).toBeInTheDocument();
  expect(calls.some((c) => c.path.startsWith("/receipts"))).toBe(false);
});

it("sends a notice with lots and expiry where the item needs them, in South African time", async () => {
  expect(sast("2026-11-02T08:30")).toBe("2026-11-02T08:30:00+02:00");
  const durban = { ...sites.items[0]!, id: "id-DBN", code: "DBN", name: "Durban" };
  const calls = mockApi({
    "GET /sites": { json: { items: [...sites.items, durban], next_cursor: null } },
    "GET /items": { json: items },
    // Stock sits at Teakwood, so the form starts there although Durban sorts first.
    "GET /inventory/summary": { json: { items: [{ site_code: "TEAK" }], truncated: false } },
    "POST /inbound-notices": { status: 201, json: { ...notice, id: "n2" } },
  });
  renderAt("/inbound/new", <NewInbound />, "/inbound/new", ["/inbound/:id"]);

  await userEvent.type(await screen.findByLabelText("Expected arrival"), "2026-11-02T08:30");
  await userEvent.selectOptions(screen.getByLabelText("Type"), "return");
  await userEvent.type(screen.getByLabelText("SKU, line 1"), "JUICE");
  await userEvent.type(screen.getByLabelText("Quantity, line 1"), "5");
  expect(screen.getByLabelText("Lot, line 1")).toBeRequired();
  expect(screen.getByLabelText("Expiry, line 1")).toBeRequired();
  await userEvent.type(screen.getByLabelText("Lot, line 1"), "L7");
  await userEvent.type(screen.getByLabelText("Expiry, line 1"), "2027-06-30");
  await userEvent.click(screen.getByRole("button", { name: "Send notice" }));

  expect(await screen.findByText("at /inbound/:id")).toBeInTheDocument();
  expect(calls.find((c) => c.method === "POST")!.body).toEqual({
    site_code: "TEAK",
    kind: "return",
    client_ref: "",
    expected_at: "2026-11-02T08:30:00+02:00",
    notes: "",
    lines: [{ sku: "JUICE", qty: "5", uom: "", lot: "L7", expiry: "2027-06-30" }],
  });
});
