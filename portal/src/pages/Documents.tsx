import { useInfiniteQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useSearchParams } from "react-router";

import { documentUrl, get, query } from "../api/client";
import type { DocumentPage } from "../api/types";
import { ErrorBox, Loading } from "../components/ui";
import { DOCUMENT_KIND, fileSize, label, when } from "../format";

// What a client looks for, mapped to the document kinds behind it. Statements arrive with billing (M4).
const ALL = { text: "All documents", kinds: [] as string[] };
const GROUPS: Record<string, { text: string; kinds: string[] }> = {
  "": ALL,
  pod: { text: "Proof of delivery", kinds: ["pod_signature", "pod_photo"] },
  receipt: { text: "Receipt photos", kinds: ["receipt_photo"] },
  upload: { text: "Files you uploaded", kinds: ["import_file"] },
};

export function Documents() {
  const [params, setParams] = useSearchParams();
  const group = params.get("show") ?? "";
  const q = params.get("q") ?? "";
  const from = params.get("from") ?? "";
  const to = params.get("to") ?? "";
  const [search, setSearch] = useState(q);
  const kinds = (GROUPS[group] ?? ALL).kinds;

  const docs = useInfiniteQuery({
    queryKey: ["documents", group, q, from, to],
    queryFn: ({ pageParam }) =>
      get<DocumentPage>(
        `/documents${query({ kind: kinds, q, captured_from: from, captured_to: to, limit: 50, cursor: pageParam })}`,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const rows = docs.data?.pages.flatMap((p) => p.items) ?? [];

  function update(next: Record<string, string>) {
    const merged = { show: group, q, from, to, ...next };
    setParams(Object.fromEntries(Object.entries(merged).filter(([, v]) => v)));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    update({ q: search.trim() });
  }

  return (
    <>
      <h1>Documents</h1>
      <form className="filters" onSubmit={submit} role="search">
        <div>
          <label htmlFor="show">Show</label>
          <select id="show" value={group} onChange={(e) => update({ show: e.target.value })}>
            {Object.entries(GROUPS).map(([value, g]) => (
              <option key={value} value={value}>
                {g.text}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="from">From</label>
          <input id="from" type="date" value={from} max={to || undefined} onChange={(e) => update({ from: e.target.value })} />
        </div>
        <div>
          <label htmlFor="to">To</label>
          <input id="to" type="date" value={to} min={from || undefined} onChange={(e) => update({ to: e.target.value })} />
        </div>
        <div>
          <label htmlFor="q">Order, shipment, notice, receipt or your reference</label>
          <input id="q" value={search} onChange={(e) => setSearch(e.target.value)} maxLength={60} />
        </div>
        <button type="submit" className="secondary">
          Search
        </button>
      </form>
      <ErrorBox error={docs.error} />
      {docs.isPending ? (
        <Loading />
      ) : rows.length === 0 ? (
        <p className="muted">No documents to show.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">
                  <span className="sr-only">Preview</span>
                </th>
                <th scope="col">Type</th>
                <th scope="col">For</th>
                <th scope="col">Captured</th>
                <th scope="col">File</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((d) => (
                <tr key={d.id}>
                  <td>
                    {d.mime.startsWith("image/") ? (
                      <a href={documentUrl(d.id)}>
                        <img className="thumb" src={documentUrl(d.id)} alt={label(DOCUMENT_KIND, d.kind)} loading="lazy" />
                      </a>
                    ) : null}
                  </td>
                  <td>{label(DOCUMENT_KIND, d.kind)}</td>
                  <td className="wrap">{d.reference || "—"}</td>
                  <td>{when(d.captured_at)}</td>
                  <td>
                    <a href={documentUrl(d.id)}>{d.original_name || "Download"}</a>{" "}
                    <span className="muted">{fileSize(d.size_bytes)}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {docs.hasNextPage && (
        <div className="actions">
          <button
            type="button"
            className="secondary"
            disabled={docs.isFetchingNextPage}
            onClick={() => void docs.fetchNextPage()}
          >
            Show more
          </button>
        </div>
      )}
    </>
  );
}
