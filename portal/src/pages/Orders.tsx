import { useInfiniteQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import { get, query } from "../api/client";
import type { OrderPage } from "../api/types";
import { ErrorBox, Loading, Tag } from "../components/ui";
import { day, ORDER_STATUS, when } from "../format";

export function Orders() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const q = params.get("q") ?? "";
  const [search, setSearch] = useState(q);
  const orders = useInfiniteQuery({
    queryKey: ["orders", status, q],
    queryFn: ({ pageParam }) =>
      get<OrderPage>(`/orders${query({ newest_first: true, limit: 50, status, q, cursor: pageParam })}`),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const rows = orders.data?.pages.flatMap((p) => p.items) ?? [];

  function update(next: Record<string, string>) {
    const merged = { status, q, ...next };
    setParams(Object.fromEntries(Object.entries(merged).filter(([, v]) => v)));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    update({ q: search.trim() });
  }

  return (
    <>
      <div className="title-row">
        <h1>Orders</h1>
        <div className="actions">
          <Link className="button" to="/orders/new">
            New order
          </Link>
          <Link className="button secondary" to="/orders/upload">
            Upload CSV
          </Link>
        </div>
      </div>
      <form className="filters" onSubmit={submit} role="search">
        <div>
          <label htmlFor="status">Show</label>
          <select id="status" value={status} onChange={(e) => update({ status: e.target.value })}>
            <option value="">All orders</option>
            {Object.entries(ORDER_STATUS).map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="q">Order number or your reference</label>
          <input id="q" value={search} onChange={(e) => setSearch(e.target.value)} maxLength={60} />
        </div>
        <button type="submit" className="secondary">
          Search
        </button>
      </form>
      <ErrorBox error={orders.error} />
      {orders.isPending ? (
        <Loading />
      ) : rows.length === 0 ? (
        <p className="muted">No orders to show.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Order</th>
                <th scope="col">Your reference</th>
                <th scope="col">Deliver to</th>
                <th scope="col">Required by</th>
                <th scope="col">Status</th>
                <th scope="col">Placed</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((o) => (
                <tr key={o.id}>
                  <td>
                    <Link to={`/orders/${o.id}`}>{o.number}</Link>
                  </td>
                  <td>{o.client_ref || "—"}</td>
                  <td>{o.ship_to_name}</td>
                  <td>{day(o.required_by)}</td>
                  <td>
                    <Tag value={o.status} labels={ORDER_STATUS} />
                  </td>
                  <td>{when(o.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {orders.hasNextPage && (
        <div className="actions">
          <button
            type="button"
            className="secondary"
            disabled={orders.isFetchingNextPage}
            onClick={() => void orders.fetchNextPage()}
          >
            Show more
          </button>
        </div>
      )}
    </>
  );
}
