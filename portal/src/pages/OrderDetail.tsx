import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router";

import { get, post, query } from "../api/client";
import { useSites } from "../api/lookups";
import type { Order, OrderEvent, ShipmentPage } from "../api/types";
import { Card, ErrorBox, Loading, Tag } from "../components/ui";
import { day, label, ORDER_STATUS, qty, SHIPMENT_STATUS, when } from "../format";

function Cancel({ order }: { order: Order }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const cancel = useMutation({
    mutationFn: () => post<Order>(`/orders/${order.id}/cancel`, { reason: reason.trim() }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["order", order.id], updated);
      void queryClient.invalidateQueries({ queryKey: ["order-timeline", order.id] });
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
      void queryClient.invalidateQueries({ queryKey: ["order-counts"] });
      setOpen(false);
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    cancel.mutate();
  }

  if (!open) {
    return (
      <div className="actions">
        <button type="button" className="secondary danger" onClick={() => setOpen(true)}>
          Cancel order
        </button>
      </div>
    );
  }
  return (
    <form className="panel" onSubmit={submit}>
      <h2>Cancel {order.number}</h2>
      <p className="muted">Reserved stock is released. This can't be undone; place a new order if you change your mind.</p>
      <ErrorBox error={cancel.error} />
      <label htmlFor="reason">Reason</label>
      <input id="reason" required value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      <div className="actions">
        <button type="submit" className="danger" disabled={cancel.isPending || !reason.trim()}>
          {cancel.isPending ? "Cancelling…" : "Cancel order"}
        </button>
        <button type="button" className="secondary" onClick={() => setOpen(false)}>
          Keep it
        </button>
      </div>
    </form>
  );
}

export function OrderDetail() {
  const { id = "" } = useParams();
  const sites = useSites();
  const order = useQuery({
    queryKey: ["order", id],
    queryFn: () => get<Order>(`/orders/${encodeURIComponent(id)}`),
  });
  const timeline = useQuery({
    queryKey: ["order-timeline", id],
    queryFn: () => get<OrderEvent[]>(`/orders/${encodeURIComponent(id)}/timeline`),
  });
  const shipment = useQuery({
    queryKey: ["order-shipment", id],
    queryFn: async () => (await get<ShipmentPage>(`/shipments${query({ sales_order_id: id })}`)).items[0] ?? null,
  });
  const o = order.data;

  return (
    <>
      <p>
        <Link to="/orders">← Orders</Link>
      </p>
      <ErrorBox error={order.error} />
      {order.isPending && <Loading />}
      {o && (
        <>
          <h1>Order {o.number}</h1>
          <div className="cards">
            <Card title="Status">
              <Tag value={o.status} labels={ORDER_STATUS} />
            </Card>
            <Card title="Your reference">{o.client_ref || "—"}</Card>
            <Card title="Ships from">{sites.data?.byId.get(o.site_id)?.name ?? "—"}</Card>
            <Card title="Deliver to">{o.ship_to.name}</Card>
            <Card title="Required by">{day(o.required_by)}</Card>
            <Card title="Placed">{when(o.created_at)}</Card>
          </div>
          {o.status === "cancelled" && o.cancelled_reason && (
            <p>
              <strong>Cancelled:</strong> {o.cancelled_reason}
            </p>
          )}
          {shipment.data && (
            <p>
              Delivery: <Tag value={shipment.data.status} labels={SHIPMENT_STATUS} />{" "}
              <Link to={`/shipments/${shipment.data.id}`}>Track shipment {shipment.data.number}</Link>
            </p>
          )}
          <h2>Delivery details</h2>
          <dl className="details">
            <dt>Address</dt>
            <dd>{o.ship_to.address || "—"}</dd>
            <dt>Contact</dt>
            <dd>{[o.contact_name, o.contact_phone].filter(Boolean).join(", ") || "—"}</dd>
            <dt>Instructions</dt>
            <dd>{o.delivery_instructions || "—"}</dd>
            <dt>Notes</dt>
            <dd>{o.notes || "—"}</dd>
          </dl>
          <h2>Lines</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">SKU</th>
                  <th scope="col" className="num">
                    Quantity
                  </th>
                  <th scope="col">Unit</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {o.lines.map((line) => (
                  <tr key={line.id}>
                    <td>{line.line_no}</td>
                    <td className="mono">{line.sku}</td>
                    <td className="num">{qty(line.qty)}</td>
                    <td>{line.uom}</td>
                    <td>{label({}, line.status)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h2>Timeline</h2>
          <ErrorBox error={timeline.error} />
          <ol className="timeline">
            {(timeline.data ?? []).map((e, i) => (
              <li key={`${e.status}-${i}`}>
                <div>{label(ORDER_STATUS, e.status)}</div>
                <div className="when">{when(e.at)}</div>
              </li>
            ))}
          </ol>
          {o.can_cancel && <Cancel order={o} />}
        </>
      )}
    </>
  );
}
