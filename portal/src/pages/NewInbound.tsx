import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";

import { post } from "../api/client";
import { useItems, usePreferredWarehouse, useSites } from "../api/lookups";
import type { Notice, NoticeIn } from "../api/types";
import { ErrorBox, Loading } from "../components/ui";
import { INBOUND_KIND } from "../format";

type Line = { key: number; sku: string; qty: string; uom: string; lot: string; expiry: string };

let nextKey = 1;
const blankLine = (): Line => ({ key: nextKey++, sku: "", qty: "", uom: "", lot: "", expiry: "" });

/** A datetime-local value ("2026-11-02T08:00") as South African time; the country has one zone and no DST. */
export const sast = (local: string) => `${local}:00+02:00`;

export function NewInbound() {
  const sites = useSites();
  const items = useItems();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [siteChoice, setSiteCode] = useState("");
  const preferred = usePreferredWarehouse();
  const siteCode = siteChoice || preferred;
  const [kind, setKind] = useState<NoticeIn["kind"]>("purchase");
  const [clientRef, setClientRef] = useState("");
  const [expectedAt, setExpectedAt] = useState("");
  const [notes, setNotes] = useState("");
  const [lines, setLines] = useState<Line[]>([blankLine()]);

  const catalogue = new Map((items.data ?? []).map((i) => [i.sku, i]));
  const unknown = lines.filter((l) => l.sku.trim() && items.data && !catalogue.has(l.sku.trim())).map((l) => l.sku);

  const send = useMutation({
    mutationFn: (body: NoticeIn) => post<Notice>("/inbound-notices", body),
    onSuccess: (notice) => {
      void queryClient.invalidateQueries({ queryKey: ["notices"] });
      navigate(`/inbound/${notice.id}`);
    },
  });

  function setLine(key: number, change: Partial<Line>) {
    setLines((current) => current.map((l) => (l.key === key ? { ...l, ...change } : l)));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    send.mutate({
      site_code: siteCode,
      kind,
      client_ref: clientRef.trim(),
      expected_at: sast(expectedAt),
      notes: notes.trim(),
      lines: lines.map((l) => ({
        sku: l.sku.trim(),
        qty: l.qty,
        uom: l.uom.trim(),
        lot: l.lot.trim(),
        expiry: l.expiry || null,
      })),
    });
  }

  if (sites.isPending) return <Loading />;
  return (
    <>
      <p>
        <Link to="/inbound">← Inbound</Link>
      </p>
      <h1>New inbound notice</h1>
      <ErrorBox error={sites.error ?? items.error} />
      <form className="order-form" onSubmit={submit}>
        <fieldset>
          <legend>Delivery</legend>
          <div className="grid">
            <div>
              <label htmlFor="site">Warehouse</label>
              <select id="site" required value={siteCode} onChange={(e) => setSiteCode(e.target.value)}>
                {sites.data?.warehouses.map((s) => (
                  <option key={s.id} value={s.code}>
                    {s.name} ({s.code})
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="kind">Type</label>
              <select id="kind" value={kind} onChange={(e) => setKind(e.target.value as NoticeIn["kind"])}>
                {Object.entries(INBOUND_KIND).map(([value, text]) => (
                  <option key={value} value={value}>
                    {text}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="expected_at">Expected arrival</label>
              <input
                id="expected_at"
                type="datetime-local"
                required
                value={expectedAt}
                onChange={(e) => setExpectedAt(e.target.value)}
              />
            </div>
            <div>
              <label htmlFor="client_ref">Your reference</label>
              <input id="client_ref" maxLength={60} value={clientRef} onChange={(e) => setClientRef(e.target.value)} />
            </div>
          </div>
        </fieldset>

        <fieldset>
          <legend>Items</legend>
          <datalist id="skus">
            {items.data?.map((i) => (
              <option key={i.id} value={i.sku}>
                {i.description}
              </option>
            ))}
          </datalist>
          <div className="table-wrap">
            <table className="lines">
              <thead>
                <tr>
                  <th scope="col">SKU</th>
                  <th scope="col">Quantity</th>
                  <th scope="col">Unit (optional)</th>
                  <th scope="col">Lot</th>
                  <th scope="col">Expiry</th>
                  <th scope="col">
                    <span className="sr-only">Remove</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {lines.map((line, index) => {
                  const item = catalogue.get(line.sku.trim());
                  return (
                    <tr key={line.key}>
                      <td>
                        <input
                          aria-label={`SKU, line ${index + 1}`}
                          list="skus"
                          required
                          value={line.sku}
                          onChange={(e) => setLine(line.key, { sku: e.target.value })}
                        />
                      </td>
                      <td>
                        <input
                          aria-label={`Quantity, line ${index + 1}`}
                          type="number"
                          inputMode="decimal"
                          min="0.001"
                          step="any"
                          required
                          value={line.qty}
                          onChange={(e) => setLine(line.key, { qty: e.target.value })}
                        />
                      </td>
                      <td>
                        <input
                          aria-label={`Unit, line ${index + 1}`}
                          maxLength={10}
                          placeholder={item?.base_uom ?? "Base unit"}
                          value={line.uom}
                          onChange={(e) => setLine(line.key, { uom: e.target.value })}
                        />
                      </td>
                      <td>
                        <input
                          aria-label={`Lot, line ${index + 1}`}
                          maxLength={40}
                          required={item?.lot_tracked}
                          value={line.lot}
                          onChange={(e) => setLine(line.key, { lot: e.target.value })}
                        />
                      </td>
                      <td>
                        <input
                          aria-label={`Expiry, line ${index + 1}`}
                          type="date"
                          required={item?.expiry_tracked}
                          value={line.expiry}
                          onChange={(e) => setLine(line.key, { expiry: e.target.value })}
                        />
                      </td>
                      <td>
                        <button
                          type="button"
                          className="secondary"
                          disabled={lines.length === 1}
                          aria-label={`Remove line ${index + 1}`}
                          onClick={() => setLines((current) => current.filter((l) => l.key !== line.key))}
                        >
                          ×
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {unknown.length > 0 && <p className="muted">Not in your catalogue: {unknown.join(", ")}</p>}
          <div className="actions">
            <button
              type="button"
              className="secondary"
              disabled={lines.length >= 500}
              onClick={() => setLines((current) => [...current, blankLine()])}
            >
              Add line
            </button>
          </div>
        </fieldset>

        <label htmlFor="notes">Notes for the warehouse</label>
        <textarea id="notes" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />

        <ErrorBox error={send.error} />
        <div className="actions">
          <button type="submit" disabled={send.isPending}>
            {send.isPending ? "Sending…" : "Send notice"}
          </button>
        </div>
      </form>
    </>
  );
}
