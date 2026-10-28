import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { get, query } from "../api/client";
import type { ClientCase, ClientCasePage, OrderCounts, ShipmentPage, StockSummary } from "../api/types";
import { ErrorBox, Loading, Tag } from "../components/ui";
import { CASE_KIND, label, OPEN_ORDER_STATUSES, ORDER_STATUS, qty, SHIPMENT_STATUS, today, when } from "../format";

const PAGE = 200;
const count = (page: ShipmentPage | undefined) =>
  page ? `${page.items.length}${page.next_cursor ? "+" : ""}` : "…";

function caseLink(c: ClientCase): string | null {
  if (c.entity_type === "oms.sales_order") return `/orders/${c.entity_id}`;
  return null;
}

function StockSummaryCard() {
  const stock = useQuery({
    queryKey: ["stock", "", ""],
    queryFn: () => get<StockSummary>("/inventory/summary"),
  });
  const rows = stock.data?.items ?? [];
  const sum = (pick: (r: (typeof rows)[number]) => string) => rows.reduce((total, r) => total + Number(pick(r)), 0);
  return (
    <section>
      <h2>
        <Link to="/stock">Stock</Link>
      </h2>
      <ErrorBox error={stock.error} />
      {stock.isPending ? (
        <Loading />
      ) : (
        <div className="cards">
          <div className="card">
            <div className="card-label">SKUs in stock</div>
            <div className="card-value">
              {new Set(rows.map((r) => r.item_id)).size}
              {stock.data?.truncated ? "+" : ""}
            </div>
          </div>
          <div className="card">
            <div className="card-label">Units available</div>
            <div className="card-value">{qty(sum((r) => r.qty_available))}</div>
          </div>
          <div className="card">
            <div className="card-label">Units allocated</div>
            <div className="card-value">{qty(sum((r) => r.qty_allocated))}</div>
          </div>
          <div className="card">
            <div className="card-label">Not available (quarantine, damaged)</div>
            <div className="card-value">{qty(sum((r) => (r.status === "available" ? "0" : r.qty_on_hand)))}</div>
          </div>
        </div>
      )}
    </section>
  );
}

export function Home() {
  const day = today();
  const counts = useQuery({ queryKey: ["order-counts"], queryFn: () => get<OrderCounts>("/orders/status-counts") });
  const onTheRoad = useQuery({
    queryKey: ["home-in-transit"],
    queryFn: () => get<ShipmentPage>(`/shipments${query({ status: "in_transit", newest_first: true, limit: PAGE })}`),
  });
  const delivered = useQuery({
    queryKey: ["home-delivered", day],
    queryFn: () => get<ShipmentPage>(`/shipments${query({ delivered_on: day, newest_first: true, limit: PAGE })}`),
  });
  const cases = useQuery({ queryKey: ["client-cases"], queryFn: () => get<ClientCasePage>("/client/exceptions") });
  const deliveries = [...(onTheRoad.data?.items ?? []), ...(delivered.data?.items ?? [])];

  return (
    <>
      <h1>Home</h1>
      <section>
        <h2>
          <Link to="/orders">Orders</Link>
        </h2>
        <ErrorBox error={counts.error} />
        <div className="cards">
          {OPEN_ORDER_STATUSES.map((status) => (
            <Link key={status} className="card link-card" to={`/orders?status=${status}`}>
              <div className="card-label">{label(ORDER_STATUS, status)}</div>
              <div className="card-value">{counts.data?.counts[status] ?? "…"}</div>
            </Link>
          ))}
          <Link className="card link-card" to="/orders?status=delivered">
            <div className="card-label">Delivered, last 7 days</div>
            <div className="card-value">{counts.data?.counts.delivered ?? "…"}</div>
          </Link>
        </div>
      </section>

      <section>
        <h2>
          <Link to="/shipments">Deliveries today</Link>
        </h2>
        <ErrorBox error={onTheRoad.error ?? delivered.error} />
        <div className="cards">
          <div className="card">
            <div className="card-label">On the road</div>
            <div className="card-value">{count(onTheRoad.data)}</div>
          </div>
          <div className="card">
            <div className="card-label">Delivered today</div>
            <div className="card-value">{count(delivered.data)}</div>
          </div>
        </div>
        {deliveries.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Shipment</th>
                  <th scope="col">Your reference</th>
                  <th scope="col">Deliver to</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {deliveries.slice(0, 20).map((s) => (
                  <tr key={s.id}>
                    <td>
                      <Link to={`/shipments/${s.id}`}>{s.number}</Link>
                    </td>
                    <td>{s.client_ref || s.order_number}</td>
                    <td>{s.ship_to_name}</td>
                    <td>
                      <Tag value={s.status} labels={SHIPMENT_STATUS} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <StockSummaryCard />

      <section>
        <h2>Needs attention</h2>
        <ErrorBox error={cases.error} />
        {cases.isPending ? (
          <Loading />
        ) : (cases.data?.items.length ?? 0) === 0 ? (
          <p className="muted">Nothing open. Short orders, receipt differences and delivery problems appear here.</p>
        ) : (
          <ul className="cases">
            {cases.data?.items.map((c) => {
              const to = caseLink(c);
              return (
                <li key={c.id}>
                  <Tag value={c.severity} /> <strong>{label(CASE_KIND, c.kind)}</strong>:{" "}
                  {to ? <Link to={to}>{c.summary}</Link> : c.summary} <span className="muted">{when(c.created_at)}</span>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </>
  );
}
