import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { mockApi } from "../test/fetch";
import { renderAt } from "../test/render";
import { NewOrder } from "./NewOrder";
import { OrderDetail } from "./OrderDetail";
import { UploadOrders } from "./UploadOrders";

const site = (code: string, name: string, kind: string, address = "") => ({
  id: `id-${code}`,
  code,
  name,
  kind,
  address,
  is_active: true,
});
const sites = {
  items: [
    site("TEAK", "Teakwood", "warehouse"),
    site("SDB-SAND", "Sandton Store", "delivery_address", "1 Rivonia Rd"),
  ],
  next_cursor: null,
};
const items = {
  items: [{ id: "i1", sku: "WATER", description: "Water 500 ml", base_uom: "EA" }],
  next_cursor: null,
};
const order = {
  id: "o1",
  number: "SO-2026-000001",
  client_id: "c1",
  site_id: "id-TEAK",
  client_ref: "PO-9",
  ship_to: { id: "id-SDB-SAND", code: "SDB-SAND", name: "Sandton Store", address: "1 Rivonia Rd" },
  required_by: null,
  status: "allocated",
  can_cancel: true,
  contact_name: "",
  contact_phone: "",
  delivery_instructions: "",
  notes: "",
  cancelled_reason: "",
  created_at: "2026-10-28T08:00:00Z",
  lines: [{ id: "l1", line_no: 1, item_id: "i1", sku: "WATER", qty: "6.000", uom: "EA", base_qty: "6.000", status: "open" }],
};

afterEach(() => vi.unstubAllGlobals());

it("places an order to a saved address", async () => {
  const calls = mockApi({
    "GET /sites": { json: sites },
    "GET /items": { json: items },
    "POST /orders": { status: 201, json: order },
  });
  renderAt("/orders/new", <NewOrder />, "/orders/new", ["/orders/:id"]);

  await userEvent.type(await screen.findByLabelText("Your reference"), "PO-9");
  await userEvent.type(screen.getByLabelText("SKU, line 1"), "WATER");
  await userEvent.type(screen.getByLabelText("Quantity, line 1"), "6");
  await userEvent.click(screen.getByRole("button", { name: "Add line" }));
  await userEvent.type(screen.getByLabelText("SKU, line 2"), "MYSTERY");
  await userEvent.type(screen.getByLabelText("Quantity, line 2"), "1");
  expect(screen.getByText("Not in your catalogue: MYSTERY")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Remove line 2" }));
  await userEvent.click(screen.getByRole("button", { name: "Place order" }));

  expect(await screen.findByText("at /orders/:id")).toBeInTheDocument();
  const placed = calls.find((c) => c.method === "POST")!;
  expect(placed.body).toMatchObject({
    site_code: "TEAK",
    client_ref: "PO-9",
    ship_to_code: "SDB-SAND",
    ship_to: null,
    required_by: null,
    lines: [{ sku: "WATER", qty: "6", uom: "" }],
  });
  expect(placed.headers["Idempotency-Key"]).toBeTruthy();
});

it("places an order to a new address and shows what the API refused", async () => {
  const calls = mockApi({
    "GET /sites": { json: sites },
    "GET /items": { json: items },
    "POST /orders": {
      status: 422,
      json: { title: "Validation failed", errors: [{ field: "body.lines.0.qty", message: "Input should be greater than 0" }] },
    },
  });
  renderAt("/orders/new", <NewOrder />, "/orders/new");

  await userEvent.click(await screen.findByLabelText(/A new address/));
  await userEvent.type(screen.getByLabelText("Name"), "Midrand DC");
  await userEvent.type(screen.getByLabelText("Street address"), "5 Main Rd");
  await userEvent.type(screen.getByLabelText("SKU, line 1"), "WATER");
  await userEvent.type(screen.getByLabelText("Quantity, line 1"), "2");
  await userEvent.click(screen.getByRole("button", { name: "Place order" }));

  expect(await screen.findByText("lines.0.qty: Input should be greater than 0")).toBeInTheDocument();
  expect(calls.find((c) => c.method === "POST")!.body).toMatchObject({
    ship_to_code: "",
    ship_to: { name: "Midrand DC", address: "5 Main Rd" },
  });
});

it("lists every rejected row of a CSV upload, then imports a fixed file", async () => {
  let attempt = 0;
  const calls = mockApi({
    "GET /sites": { json: sites },
    "GET /items": { json: items },
    "POST /orders:import": () =>
      ++attempt === 1
        ? {
            status: 422,
            json: {
              title: "Invalid CSV rows",
              detail: "2 error(s); nothing was imported.",
              errors: [
                { row: 3, field: "qty", message: "Input should be greater than 0" },
                { row: 5, field: "(row)", message: "Unknown SKU NOPE" },
              ],
            },
          }
        : { status: 201, json: { created: 1, document_id: "d1", orders: [{ id: "o1", number: "SO-1", client_ref: "PO-1" }] } },
  });
  renderAt("/orders/upload", <UploadOrders />, "/orders/upload");

  const file = new File(["client_ref,site_code\n"], "orders.csv", { type: "text/csv" });
  await userEvent.upload(screen.getByLabelText("CSV file (UTF-8)"), file);
  await userEvent.click(screen.getByRole("button", { name: "Upload" }));

  const table = await screen.findByRole("table");
  const rows = within(table).getAllByRole("row").slice(1).map((r) => r.textContent);
  expect(rows).toEqual(["3qtyInput should be greater than 0", "5whole orderUnknown SKU NOPE"]);
  expect(screen.getByText("2 error(s); nothing was imported.")).toBeInTheDocument();
  const sent = calls.find((c) => c.method === "POST")!.body as FormData;
  expect((sent.get("file") as File).name).toBe("orders.csv");

  await userEvent.click(screen.getByRole("button", { name: "Upload" }));
  expect(await screen.findByText("Imported 1 order.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "SO-1" })).toHaveAttribute("href", "/orders/o1");
});

it("shows an order's timeline and delivery, and cancels it with a reason", async () => {
  const calls = mockApi({
    "GET /sites": { json: sites },
    "GET /orders/o1": { json: order },
    "GET /orders/o1/timeline": {
      json: [
        { at: "2026-10-28T08:00:00Z", status: "accepted", detail: "" },
        { at: "2026-10-28T08:00:01Z", status: "allocated", detail: "" },
      ],
    },
    "GET /shipments": { json: { items: [{ id: "s1", number: "SHP-1", status: "draft" }], next_cursor: null } },
    "POST /orders/o1/cancel": { json: { ...order, status: "cancelled", can_cancel: false, cancelled_reason: "Duplicate" } },
  });
  renderAt("/orders/:id", <OrderDetail />, "/orders/o1");

  expect(await screen.findByRole("heading", { name: "Order SO-2026-000001" })).toBeInTheDocument();
  expect(await screen.findByText("Stock reserved", { selector: "ol li div" })).toBeInTheDocument();
  expect(await screen.findByRole("link", { name: "Track shipment SHP-1" })).toHaveAttribute("href", "/shipments/s1");
  expect(screen.getByText("Teakwood")).toBeInTheDocument();
  expect(calls.find((c) => c.path.startsWith("/shipments?"))!.path).toBe("/shipments?sales_order_id=o1");

  await userEvent.click(screen.getByRole("button", { name: "Cancel order" }));
  await userEvent.type(screen.getByLabelText("Reason"), "Duplicate");
  await userEvent.click(screen.getByRole("button", { name: "Cancel order" }));

  expect(await screen.findByText("Duplicate")).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel order" })).not.toBeInTheDocument());
  expect(calls.find((c) => c.method === "POST")!.body).toEqual({ reason: "Duplicate" });
});
