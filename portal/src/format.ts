const dateTime = new Intl.DateTimeFormat("en-ZA", { dateStyle: "medium", timeStyle: "short" });
const dateOnly = new Intl.DateTimeFormat("en-ZA", { dateStyle: "medium" });

export const when = (iso: string | null | undefined) => (iso ? dateTime.format(new Date(iso)) : "—");
/** A calendar date ("2026-10-28") shown as a local date, without a time-zone shift. */
export const day = (iso: string | null | undefined) => (iso ? dateOnly.format(new Date(`${iso}T00:00:00`)) : "—");

export const SHIPMENT_STATUS: Record<string, string> = {
  draft: "Being packed",
  ready: "Ready to plan",
  planned: "Planned",
  in_transit: "In transit",
  delivered: "Delivered",
  failed: "Delivery failed",
  cancelled: "Cancelled",
};

export const TRACKING_EVENT: Record<string, string> = {
  order_received: "Order received",
  packed: "Packed",
  dispatched: "Dispatched",
  arrived: "Vehicle arrived",
  delivered: "Delivered",
  delivery_failed: "Delivery failed",
};

export const FAILURE_REASON: Record<string, string> = {
  closed: "Premises closed",
  refused: "Refused by the recipient",
  no_access: "No access",
  wrong_address: "Wrong or unknown address",
  damaged: "Goods damaged",
  out_of_time: "Ran out of time",
  other: "Other",
};

export const label = (labels: Record<string, string>, value: string) => labels[value] ?? value.replace(/_/g, " ");
