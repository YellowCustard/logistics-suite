import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { useItems } from "../api/lookups";
import type { Item } from "../api/types";
import { useIsClientAdmin } from "../auth";
import { ErrorBox, Loading } from "../components/ui";
import { downloadCsv, toCsv } from "../csv";
import { today } from "../format";

const isActive = (i: Item) => i.is_active;
const SHOW: Record<string, (i: Item) => boolean> = {
  active: isActive,
  inactive: (i) => !i.is_active,
  all: () => true,
};

/** SKU or barcode from the start, or any part of the description. */
function matches(item: Item, q: string): boolean {
  const needle = q.toLowerCase();
  return (
    item.sku.toLowerCase().startsWith(needle) ||
    item.description.toLowerCase().includes(needle) ||
    item.barcodes.some((b) => b.barcode.toLowerCase().startsWith(needle))
  );
}

export const packSizes = (item: Item) =>
  item.pack_sizes
    .filter((p) => p.factor > 1)
    .map((p) => `${p.uom} × ${p.factor}`)
    .join(", ");

export function Items() {
  const items = useItems();
  const admin = useIsClientAdmin();
  const [params, setParams] = useSearchParams();
  const show = params.get("show") ?? "active";
  const q = params.get("q") ?? "";
  const [search, setSearch] = useState(q);
  const rows = (items.data ?? []).filter((i) => (SHOW[show] ?? isActive)(i) && (!q || matches(i, q)));

  function update(next: Record<string, string>) {
    const merged = { show, q, ...next };
    setParams(Object.fromEntries(Object.entries(merged).filter(([k, v]) => v && !(k === "show" && v === "active"))));
  }

  function exportCsv() {
    const header = ["sku", "description", "base_uom", "pack_sizes", "barcodes", "units_per_pallet", "weight_kg"];
    const extra = ["volume_m3", "lot_tracked", "expiry_tracked", "active"];
    const body = rows.map((i) => [
      i.sku,
      i.description,
      i.base_uom,
      packSizes(i),
      i.barcodes.map((b) => `${b.barcode} (${b.uom})`).join("; "),
      i.units_per_pallet,
      i.weight_kg,
      i.volume_m3,
      i.lot_tracked,
      i.expiry_tracked,
      i.is_active,
    ]);
    downloadCsv(`items-${today()}.csv`, toCsv([...header, ...extra], body));
  }

  return (
    <>
      <div className="title-row">
        <h1>Items</h1>
        <div className="actions">
          {admin && (
            <>
              <Link className="button" to="/items/new">
                New item
              </Link>
              <Link className="button secondary" to="/items/upload">
                Upload CSV
              </Link>
            </>
          )}
          <button type="button" className="secondary" disabled={rows.length === 0} onClick={exportCsv}>
            Download CSV
          </button>
        </div>
      </div>
      <form
        className="filters"
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          update({ q: search.trim() });
        }}
      >
        <div>
          <label htmlFor="show">Show</label>
          <select id="show" value={show} onChange={(e) => update({ show: e.target.value })}>
            <option value="active">Active items</option>
            <option value="inactive">Inactive items</option>
            <option value="all">All items</option>
          </select>
        </div>
        <div>
          <label htmlFor="q">SKU, barcode or description</label>
          <input id="q" value={search} onChange={(e) => setSearch(e.target.value)} maxLength={64} />
        </div>
        <button type="submit" className="secondary">
          Search
        </button>
      </form>
      <ErrorBox error={items.error} />
      {items.isPending ? (
        <Loading />
      ) : rows.length === 0 ? (
        <p className="muted">No items to show.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">SKU</th>
                <th scope="col">Description</th>
                <th scope="col">Unit</th>
                <th scope="col">Pack sizes</th>
                <th scope="col">Barcodes</th>
                <th scope="col">Tracked by</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((i) => (
                <tr key={i.id}>
                  <td className="mono">
                    <Link to={`/items/${i.id}`}>{i.sku}</Link>
                    {!i.is_active && (
                      <>
                        {" "}
                        <span className="tag">Inactive</span>
                      </>
                    )}
                  </td>
                  <td>{i.description}</td>
                  <td>{i.base_uom}</td>
                  <td>{packSizes(i) || "—"}</td>
                  <td>{i.barcodes.length || "—"}</td>
                  <td>{[i.lot_tracked && "Lot", i.expiry_tracked && "Expiry"].filter(Boolean).join(", ") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted">
        {rows.length} of {items.data?.length ?? 0} items.
      </p>
    </>
  );
}
