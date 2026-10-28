import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { mockApi } from "../test/fetch";
import { renderAt } from "../test/render";
import { Documents } from "./Documents";
import { ItemDetail } from "./ItemDetail";
import { Items } from "./Items";

afterEach(() => vi.unstubAllGlobals());

const item = (over: Record<string, unknown>) => ({
  id: "i1",
  client_id: "c1",
  sku: "WATER",
  description: "Water 500ml",
  base_uom: "EA",
  weight_kg: null,
  volume_m3: null,
  units_per_pallet: 1440,
  lot_tracked: false,
  expiry_tracked: false,
  is_active: true,
  version: 3,
  pack_sizes: [
    { uom: "EA", factor: 1 },
    { uom: "CS", factor: 24 },
  ],
  barcodes: [{ id: "b1", barcode: "6001234000013", uom: "EA" }],
  ...over,
});
const catalogue = {
  items: [
    item({}),
    item({ id: "i2", sku: "JUICE", description: "Orange juice", lot_tracked: true, barcodes: [], pack_sizes: [] }),
    item({ id: "i3", sku: "OLD", description: "Discontinued soda", is_active: false, barcodes: [] }),
  ],
  next_cursor: null,
};

it("lists active items, finds them by barcode, and offers edits only to admins", async () => {
  mockApi({ "GET /items": { json: catalogue } });
  renderAt("/items", <Items />, "/items");
  expect(await screen.findByRole("link", { name: "JUICE" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "OLD" })).not.toBeInTheDocument();
  expect(screen.getByText("CS × 24", { selector: "td" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "New item" })).not.toBeInTheDocument();

  await userEvent.type(screen.getByLabelText("SKU, barcode or description"), "600123");
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(await screen.findByText("1 of 3 items.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "WATER" })).toBeInTheDocument();

  await userEvent.selectOptions(screen.getByLabelText("Show"), "inactive");
  await userEvent.clear(screen.getByLabelText("SKU, barcode or description"));
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(await screen.findByRole("link", { name: "OLD" })).toBeInTheDocument();
});

it("shows admins the new-item and upload actions", async () => {
  mockApi({ "GET /items": { json: catalogue } });
  renderAt("/items", <Items />, "/items", [], ["client_admin"]);
  expect(await screen.findByRole("link", { name: "New item" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Upload CSV" })).toBeInTheDocument();
});

it("saves item details with the version it was read at, and adds and removes barcodes", async () => {
  vi.stubGlobal("confirm", () => true);
  let current = item({});
  const calls = mockApi({
    "GET /items/i1": () => ({ json: current }),
    "PATCH /items/i1": (call) => {
      current = { ...current, ...(call.body as object), version: current.version + 1 };
      return { json: current };
    },
    "POST /items/i1/barcodes": (call) => {
      const body = call.body as { barcode: string; uom: string };
      if (body.barcode === "16001234000010") return { status: 409, json: { title: "Barcode in use", detail: "16001234000010 already belongs to JUICE." } };
      current = { ...current, barcodes: [...current.barcodes, { id: "b2", ...body }] };
      return { status: 201, json: current };
    },
    "DELETE /items/i1/barcodes/b1": () => {
      current = { ...current, barcodes: current.barcodes.filter((b) => b.id !== "b1") };
      return { json: current };
    },
  });
  renderAt("/items/:id", <ItemDetail />, "/items/i1", [], ["client_admin"]);

  const description = await screen.findByLabelText("Description");
  await userEvent.clear(description);
  await userEvent.type(description, "Still water 500ml");
  await userEvent.type(screen.getByLabelText("Weight per EA (kg)"), "0.52");
  await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
  const saved = calls.find((c) => c.method === "PATCH")!;
  expect(saved.headers["If-Match"]).toBe("3");
  expect(saved.body).toEqual({ description: "Still water 500ml", weight_kg: "0.52", volume_m3: null, units_per_pallet: 1440 });

  await userEvent.type(screen.getByLabelText(/New barcode/), "16001234000010");
  await userEvent.selectOptions(screen.getByLabelText("Unit"), "CS");
  await userEvent.click(screen.getByRole("button", { name: "Add barcode" }));
  expect(await screen.findByText("16001234000010 already belongs to JUICE.")).toBeInTheDocument();

  await userEvent.clear(screen.getByLabelText(/New barcode/));
  await userEvent.type(screen.getByLabelText(/New barcode/), "16001234000027");
  await userEvent.click(screen.getByRole("button", { name: "Add barcode" }));
  expect(await screen.findByText("16001234000027")).toBeInTheDocument();
  expect(calls.find((c) => c.method === "POST" && (c.body as { barcode: string }).barcode === "16001234000027")?.body).toEqual({
    barcode: "16001234000027",
    uom: "CS",
  });

  await userEvent.click(screen.getByRole("button", { name: "Remove barcode 6001234000013" }));
  await waitFor(() => expect(screen.queryByText("6001234000013")).not.toBeInTheDocument());
});

it("shows items read-only to users without the admin role", async () => {
  mockApi({ "GET /items/i1": { json: item({}) } });
  renderAt("/items/:id", <ItemDetail />, "/items/i1");
  expect(await screen.findByText(/Your account administrator can change items/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Add barcode" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Remove barcode/ })).not.toBeInTheDocument();
});

it("searches documents by kind group and reference and links each file", async () => {
  const doc = {
    id: "d1",
    client_id: "c1",
    kind: "pod_signature",
    original_name: "signature.png",
    sha256: "0".repeat(64),
    mime: "image/png",
    size_bytes: 2048,
    captured_at: "2026-11-02T08:00:00Z",
    linked_type: "tms.stop",
    linked_id: "s1",
    reference: "SHP-1 · SO-1 · PO-9",
  };
  const calls = mockApi({ "GET /documents": { json: { items: [doc], next_cursor: null } } });
  renderAt("/documents", <Documents />, "/documents");
  expect(await screen.findByText("SHP-1 · SO-1 · PO-9")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "signature.png" })).toHaveAttribute("href", "/api/v1/documents/d1/download");
  expect(screen.getByRole("img", { name: "POD signature" })).toHaveAttribute("src", "/api/v1/documents/d1/download");
  expect(screen.getByText("2 KB")).toBeInTheDocument();

  await userEvent.selectOptions(screen.getByLabelText("Show"), "pod");
  await userEvent.type(screen.getByLabelText(/Order, shipment/), "PO-9");
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
  await waitFor(() =>
    expect(calls.at(-1)?.path).toBe("/documents?kind=pod_signature&kind=pod_photo&q=PO-9&limit=50"),
  );
});
