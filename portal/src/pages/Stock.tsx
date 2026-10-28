import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useSearchParams } from "react-router";

import { get, query } from "../api/client";
import { useSites } from "../api/lookups";
import type { StockRow, StockSummary } from "../api/types";
import { downloadCsv, toCsv } from "../csv";
import { ErrorBox, Loading, Tag } from "../components/ui";
import { day, qty, STOCK_STATUS, today } from "../format";

const EXPIRY_WARNING_DAYS = 30;

function expirySoon(row: StockRow, now: string): boolean {
  if (!row.expiry) return false;
  const days = (Date.parse(row.expiry) - Date.parse(now)) / 86_400_000;
  return days <= EXPIRY_WARNING_DAYS;
}

export function Stock() {
  const [params, setParams] = useSearchParams();
  const siteId = params.get("site_id") ?? "";
  const q = params.get("q") ?? "";
  const [search, setSearch] = useState(q);
  const sites = useSites();
  const stock = useQuery({
    queryKey: ["stock", siteId, q],
    queryFn: () => get<StockSummary>(`/inventory/summary${query({ site_id: siteId, q })}`),
  });
  const rows = stock.data?.items ?? [];
  const now = today();

  function update(next: Record<string, string>) {
    const merged = { site_id: siteId, q, ...next };
    setParams(Object.fromEntries(Object.entries(merged).filter(([, v]) => v)));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    update({ q: search.trim() });
  }

  function exportCsv() {
    const header = ["site", "sku", "description", "lot", "expiry", "status", "on_hand", "allocated", "available", "uom"];
    const body = rows.map((r) => [
      r.site_code,
      r.sku,
      r.description,
      r.lot,
      r.expiry ?? "",
      r.status,
      r.qty_on_hand,
      r.qty_allocated,
      r.qty_available,
      r.base_uom,
    ]);
    downloadCsv(`stock-on-hand-${now}.csv`, toCsv(header, body));
  }

  return (
    <>
      <div className="title-row">
        <h1>Stock on hand</h1>
        <div className="actions">
          <button type="button" className="secondary" disabled={rows.length === 0} onClick={exportCsv}>
            Export CSV
          </button>
        </div>
      </div>
      <form className="filters" onSubmit={submit} role="search">
        <div>
          <label htmlFor="site">Warehouse</label>
          <select id="site" value={siteId} onChange={(e) => update({ site_id: e.target.value })}>
            <option value="">All warehouses</option>
            {sites.data?.warehouses.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="q">SKU or description</label>
          <input id="q" value={search} onChange={(e) => setSearch(e.target.value)} maxLength={60} />
        </div>
        <button type="submit" className="secondary">
          Search
        </button>
      </form>
      <p className="muted">
        Quantities are in each item's base unit. Allocated stock is reserved for your open orders; available is what new
        orders can use.
      </p>
      <ErrorBox error={stock.error} />
      {stock.data?.truncated && (
        <div className="flash bad" role="status">
          Showing the first {rows.length} rows. Choose a warehouse or search for a SKU to see the rest.
        </div>
      )}
      {stock.isPending ? (
        <Loading />
      ) : rows.length === 0 ? (
        <p className="muted">No stock to show.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Warehouse</th>
                <th scope="col">SKU</th>
                <th scope="col">Description</th>
                <th scope="col">Lot</th>
                <th scope="col">Expiry</th>
                <th scope="col">Status</th>
                <th scope="col" className="num">
                  On hand
                </th>
                <th scope="col" className="num">
                  Allocated
                </th>
                <th scope="col" className="num">
                  Available
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.site_id}-${r.item_id}-${r.lot}-${r.expiry}-${r.status}`}>
                  <td>{r.site_code}</td>
                  <td className="mono">{r.sku}</td>
                  <td>{r.description}</td>
                  <td>{r.lot || "—"}</td>
                  <td className={expirySoon(r, now) ? "warn" : undefined}>{day(r.expiry)}</td>
                  <td>
                    <Tag value={r.status} labels={STOCK_STATUS} />
                  </td>
                  <td className="num">{qty(r.qty_on_hand)}</td>
                  <td className="num">{qty(r.qty_allocated)}</td>
                  <td className="num">{qty(r.qty_available)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
