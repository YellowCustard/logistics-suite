const dateTime = new Intl.DateTimeFormat("en-ZA", { dateStyle: "medium", timeStyle: "short" });
const dateOnly = new Intl.DateTimeFormat("en-ZA", { dateStyle: "medium" });

export const when = (iso: string | null | undefined) => (iso ? dateTime.format(new Date(iso)) : "—");
/** A calendar date ("2026-10-28") shown as a local date, without a time-zone shift. */
export const day = (iso: string | null | undefined) => (iso ? dateOnly.format(new Date(`${iso}T00:00:00`)) : "—");

/** Today's date in South Africa ("2026-10-28"), the day the server's date filters use. */
export const today = () => new Intl.DateTimeFormat("en-CA", { timeZone: "Africa/Johannesburg" }).format(new Date());

const number = new Intl.NumberFormat("en-ZA", { maximumFractionDigits: 3 });
/** A decimal quantity from the API ("15.000") without trailing zeros, grouped for reading. */
export const qty = (value: string | number | null | undefined) =>
  value === null || value === undefined || value === "" ? "—" : number.format(Number(value));

export const ORDER_STATUS: Record<string, string> = {
  accepted: "Accepted",
  allocated: "Stock reserved",
  backordered: "Short of stock",
  picking: "Being picked",
  packed: "Packed",
  dispatched: "On its way",
  delivered: "Delivered",
  cancelled: "Cancelled",
};

/** Statuses of an order that is still moving, in the order it moves through them. */
export const OPEN_ORDER_STATUSES = ["accepted", "allocated", "backordered", "picking", "packed", "dispatched"];

export const STOCK_STATUS: Record<string, string> = {
  available: "Available",
  quarantine: "Quarantine",
  damaged: "Damaged",
};

export const CASE_KIND: Record<string, string> = {
  "allocation.short": "Order short of stock",
  "pick.short": "Short at picking",
  "receipt.variance": "Receipt differs from notice",
  "delivery.exception": "Delivery exception",
  "delivery.failed": "Delivery failed",
  "webhook.dead_letter": "Webhook failing",
};

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
