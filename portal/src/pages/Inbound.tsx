import { useInfiniteQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import { get, query } from "../api/client";
import { useSites } from "../api/lookups";
import type { NoticePage } from "../api/types";
import { ErrorBox, Loading, Tag } from "../components/ui";
import { INBOUND_KIND, INBOUND_STATUS, label, when } from "../format";

export function Inbound() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const q = params.get("q") ?? "";
  const [search, setSearch] = useState(q);
  const sites = useSites();
  const notices = useInfiniteQuery({
    queryKey: ["notices", status, q],
    queryFn: ({ pageParam }) =>
      get<NoticePage>(`/inbound-notices${query({ newest_first: true, limit: 50, status, q, cursor: pageParam })}`),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const rows = notices.data?.pages.flatMap((p) => p.items) ?? [];

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
        <h1>Inbound</h1>
        <div className="actions">
          <Link className="button" to="/inbound/new">
            New notice
          </Link>
          <Link className="button secondary" to="/inbound/upload">
            Upload CSV
          </Link>
        </div>
      </div>
      <p className="muted">Tell the warehouse what is arriving, then see what was received against it.</p>
      <form className="filters" onSubmit={submit} role="search">
        <div>
          <label htmlFor="status">Show</label>
          <select id="status" value={status} onChange={(e) => update({ status: e.target.value })}>
            <option value="">All notices</option>
            {Object.entries(INBOUND_STATUS).map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="q">Notice number or your reference</label>
          <input id="q" value={search} onChange={(e) => setSearch(e.target.value)} maxLength={60} />
        </div>
        <button type="submit" className="secondary">
          Search
        </button>
      </form>
      <ErrorBox error={notices.error} />
      {notices.isPending ? (
        <Loading />
      ) : rows.length === 0 ? (
        <p className="muted">No inbound notices to show.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Notice</th>
                <th scope="col">Your reference</th>
                <th scope="col">Type</th>
                <th scope="col">Warehouse</th>
                <th scope="col">Expected</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((n) => (
                <tr key={n.id}>
                  <td>
                    <Link to={`/inbound/${n.id}`}>{n.number}</Link>
                  </td>
                  <td>{n.client_ref || "—"}</td>
                  <td>{label(INBOUND_KIND, n.kind)}</td>
                  <td>{sites.data?.byId.get(n.site_id)?.name ?? "—"}</td>
                  <td>{when(n.expected_at)}</td>
                  <td>
                    <Tag value={n.status} labels={INBOUND_STATUS} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {notices.hasNextPage && (
        <div className="actions">
          <button
            type="button"
            className="secondary"
            disabled={notices.isFetchingNextPage}
            onClick={() => void notices.fetchNextPage()}
          >
            Show more
          </button>
        </div>
      )}
    </>
  );
}
