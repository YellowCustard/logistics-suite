import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { mockApi } from "../test/fetch";
import { Tracking } from "./Tracking";

const doc = (id: string, kind: string) => ({ id, kind, sha256: "ab", mime: "image/png", captured_at: "2026-10-28T09:40:00Z" });

const base = {
  shipment_id: "s1",
  number: "SHP-000001",
  order_number: "SO-000001",
  client_ref: "PO-55",
  ship_to_name: "Spar Midrand",
  required_by: "2026-10-30",
  trip_number: "TRP-000001",
  vehicle_registration: "ND 123-456",
  events: [
    { at: "2026-10-28T07:00:00Z", what: "packed", detail: "" },
    { at: "2026-10-28T08:00:00Z", what: "dispatched", detail: "" },
  ],
};

function show(tracking: unknown) {
  mockApi({ "GET /shipments/s1/tracking": { json: tracking } });
  const router = createMemoryRouter([{ path: "/shipments/:id", element: <Tracking /> }], {
    initialEntries: ["/shipments/s1"],
  });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

it("shows the ETA and stops ahead while on the road", async () => {
  show({ ...base, status: "in_transit", stops_before: 2, eta: "2026-10-28T10:15:00Z", pod: null });

  expect(await screen.findByRole("heading", { name: "Shipment SHP-000001" })).toBeInTheDocument();
  expect(screen.getByText("2 stops before yours")).toBeInTheDocument();
  expect(screen.getByText("Estimated arrival").parentElement).not.toHaveTextContent("—");
  expect(screen.getByText("Not delivered yet.")).toBeInTheDocument();
});

it("shows the proof of delivery with downloadable signature and photos, and unit outcomes", async () => {
  show({
    ...base,
    status: "delivered",
    stops_before: null,
    eta: null,
    pod: {
      shipment_id: "s1",
      trip_number: "TRP-000001",
      outcome: "delivered",
      recipient_name: "T. Nkosi",
      reason: "",
      note: "Left at receiving bay 2",
      captured_at: "2026-10-28T09:40:00Z",
      latitude: null,
      longitude: null,
      signature: doc("sig1", "pod_signature"),
      photos: [doc("ph1", "pod_photo")],
      units: [{ sscc: "000000000000000017", kind: "pallet", cartons: 12, delivery_status: "damaged" }],
    },
  });

  expect(await screen.findByRole("heading", { name: "Proof of delivery" })).toBeInTheDocument();
  expect(screen.getByText("T. Nkosi")).toBeInTheDocument();
  expect(screen.getByText(/Left at receiving bay 2/)).toBeInTheDocument();
  expect(screen.getByRole("img", { name: "Signature" })).toHaveAttribute("src", "/api/v1/documents/sig1/download");
  const downloads = screen.getAllByRole("link", { name: "Download" }).map((a) => a.getAttribute("href"));
  expect(downloads).toEqual(["/api/v1/documents/sig1/download", "/api/v1/documents/ph1/download"]);
  const row = screen.getByText("000000000000000017").closest("tr")!;
  expect(within(row).getByText(/damaged/i)).toBeInTheDocument();
});

it("explains a failed attempt", async () => {
  show({
    ...base,
    status: "failed",
    stops_before: null,
    eta: null,
    events: [...base.events, { at: "2026-10-28T09:00:00Z", what: "delivery_failed", detail: "closed" }],
    pod: {
      shipment_id: "s1",
      trip_number: "TRP-000001",
      outcome: "failed",
      recipient_name: "",
      reason: "closed",
      note: "",
      captured_at: "2026-10-28T09:00:00Z",
      latitude: null,
      longitude: null,
      signature: null,
      photos: [],
      units: [],
    },
  });
  expect(await screen.findByRole("heading", { name: "Delivery attempt" })).toBeInTheDocument();
});
