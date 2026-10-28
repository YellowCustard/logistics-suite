import { useInfiniteQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";

import { get, query } from "../api/client";
import type { ShipmentPage } from "../api/types";
import { ErrorBox, Loading, Tag } from "../components/ui";
import { day, SHIPMENT_STATUS } from "../format";

const FILTERS: [string, string][] = [
  ["", "All shipments"],
  ["in_transit", "In transit"],
  ["delivered", "Delivered"],
  ["failed", "Delivery failed"],
  ["planned", "Planned"],
  ["ready", "Ready to plan"],
  ["draft", "Being packed"],
  ["cancelled", "Cancelled"],
];

export function Shipments() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const shipments = useInfiniteQuery({
    queryKey: ["shipments", status],
    queryFn: ({ pageParam }) =>
      get<ShipmentPage>(`/shipments${query({ newest_first: true, limit: 50, status, cursor: pageParam })}`),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const rows = shipments.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <>
      <h1>Tracking</h1>
      <div className="filters">
        <div>
          <label htmlFor="status">Show</label>
          <select
            id="status"
            value={status}
            onChange={(e) => setParams(e.target.value ? { status: e.target.value } : {})}
          >
            {FILTERS.map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </select>
        </div>
      </div>
      <ErrorBox error={shipments.error} />
      {shipments.isPending ? (
        <Loading />
      ) : rows.length === 0 ? (
        <p className="muted">No shipments to show.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Shipment</th>
                <th scope="col">Your reference</th>
                <th scope="col">Deliver to</th>
                <th scope="col">Required by</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.id}>
                  <td>
                    <Link to={`/shipments/${s.id}`}>{s.number}</Link>
                  </td>
                  <td>{s.client_ref || s.order_number}</td>
                  <td>{s.ship_to_name}</td>
                  <td>{day(s.required_by)}</td>
                  <td>
                    <Tag value={s.status} labels={SHIPMENT_STATUS} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {shipments.hasNextPage && (
        <div className="actions">
          <button
            type="button"
            className="secondary"
            disabled={shipments.isFetchingNextPage}
            onClick={() => void shipments.fetchNextPage()}
          >
            Show more
          </button>
        </div>
      )}
    </>
  );
}
