import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router";

import { documentUrl, get, post, query } from "../api/client";
import { useSites } from "../api/lookups";
import type { Notice, Receipt, ReceiptPage } from "../api/types";
import { Card, ErrorBox, Loading, Tag } from "../components/ui";
import { day, INBOUND_KIND, INBOUND_STATUS, label, qty, when } from "../format";

function Cancel({ notice }: { notice: Notice }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const cancel = useMutation({
    mutationFn: () => post<Notice>(`/inbound-notices/${notice.id}/cancel`, { reason: reason.trim() }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["notice", notice.id], updated);
      void queryClient.invalidateQueries({ queryKey: ["notices"] });
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
          Cancel notice
        </button>
      </div>
    );
  }
  return (
    <form className="panel" onSubmit={submit}>
      <h2>Cancel {notice.number}</h2>
      <p className="muted">The warehouse stops expecting this delivery. This can't be undone.</p>
      <ErrorBox error={cancel.error} />
      <label htmlFor="reason">Reason</label>
      <input id="reason" required value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      <div className="actions">
        <button type="submit" className="danger" disabled={cancel.isPending || !reason.trim()}>
          {cancel.isPending ? "Cancelling…" : "Cancel notice"}
        </button>
        <button type="button" className="secondary" onClick={() => setOpen(false)}>
          Keep it
        </button>
      </div>
    </form>
  );
}

function Received({ receipt }: { receipt: Receipt }) {
  const damaged = receipt.lines.filter((l) => l.condition === "damaged");
  return (
    <>
      <h2>Receipt {receipt.number}</h2>
      <p className="muted">
        {receipt.status === "completed" ? `Completed ${when(receipt.completed_at)}` : "Being received now"}
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">SKU</th>
              <th scope="col" className="num">
                Quantity
              </th>
              <th scope="col">Unit</th>
              <th scope="col">Lot</th>
              <th scope="col">Expiry</th>
              <th scope="col">Condition</th>
            </tr>
          </thead>
          <tbody>
            {receipt.lines.map((l) => (
              <tr key={l.id}>
                <td className="mono">
                  {l.sku}
                  {!l.inbound_notice_line_id && <span className="muted"> (not on the notice)</span>}
                </td>
                <td className="num">{qty(l.qty)}</td>
                <td>{l.uom}</td>
                <td>{l.lot || "—"}</td>
                <td>{day(l.expiry)}</td>
                <td>
                  <Tag value={l.condition} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {damaged.some((l) => (l.photos ?? []).length > 0) && (
        <>
          <h2>Damage photos</h2>
          <div className="evidence">
            {damaged.flatMap((l) =>
              (l.photos ?? []).map((p, i) => (
                <figure key={p.id}>
                  <a href={documentUrl(p.id)}>
                    <img src={documentUrl(p.id)} alt={`${l.sku} damage photo ${i + 1}`} />
                  </a>
                  <figcaption>
                    {l.sku}, {qty(l.qty)} {l.uom} · <a href={documentUrl(p.id)}>Download</a>
                  </figcaption>
                </figure>
              )),
            )}
          </div>
        </>
      )}
    </>
  );
}

export function InboundDetail() {
  const { id = "" } = useParams();
  const sites = useSites();
  const notice = useQuery({
    queryKey: ["notice", id],
    queryFn: () => get<Notice>(`/inbound-notices/${encodeURIComponent(id)}`),
  });
  const receipts = useQuery({
    queryKey: ["notice-receipts", id],
    queryFn: () => get<ReceiptPage>(`/receipts${query({ inbound_notice_id: id })}`),
    enabled: notice.data !== undefined && notice.data.status !== "accepted" && notice.data.status !== "cancelled",
  });
  const n = notice.data;
  const closed = n?.status === "closed";

  return (
    <>
      <p>
        <Link to="/inbound">← Inbound</Link>
      </p>
      <ErrorBox error={notice.error} />
      {notice.isPending && <Loading />}
      {n && (
        <>
          <h1>Inbound notice {n.number}</h1>
          <div className="cards">
            <Card title="Status">
              <Tag value={n.status} labels={INBOUND_STATUS} />
            </Card>
            <Card title="Your reference">{n.client_ref || "—"}</Card>
            <Card title="Type">{label(INBOUND_KIND, n.kind)}</Card>
            <Card title="Warehouse">{sites.data?.byId.get(n.site_id)?.name ?? "—"}</Card>
            <Card title="Expected">{when(n.expected_at)}</Card>
            <Card title="Sent">{when(n.created_at)}</Card>
          </div>
          {n.status === "cancelled" && n.cancelled_reason && (
            <p>
              <strong>Cancelled:</strong> {n.cancelled_reason}
            </p>
          )}
          {n.notes && (
            <p>
              <strong>Notes:</strong> {n.notes}
            </p>
          )}
          <h2>Expected and received</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">SKU</th>
                  <th scope="col">Lot</th>
                  <th scope="col">Expiry</th>
                  <th scope="col" className="num">
                    Expected
                  </th>
                  <th scope="col" className="num">
                    Received
                  </th>
                  <th scope="col" className="num">
                    Difference
                  </th>
                  <th scope="col">Unit</th>
                </tr>
              </thead>
              <tbody>
                {n.lines.map((line) => {
                  const diff = Number(line.qty_received) - Number(line.qty);
                  return (
                    <tr key={line.id}>
                      <td>{line.line_no}</td>
                      <td className="mono">{line.sku}</td>
                      <td>{line.lot || "—"}</td>
                      <td>{day(line.expiry)}</td>
                      <td className="num">{qty(line.qty)}</td>
                      <td className="num">{n.status === "accepted" ? "—" : qty(line.qty_received)}</td>
                      <td className={`num${closed && diff !== 0 ? " warn" : ""}`}>
                        {closed ? (diff > 0 ? `+${qty(diff)}` : qty(diff)) : "—"}
                      </td>
                      <td>{line.uom}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {closed && n.lines.some((l) => Number(l.qty_received) !== Number(l.qty)) && (
            <p className="muted">The warehouse has opened a case for the difference; it shows on Home until resolved.</p>
          )}
          <ErrorBox error={receipts.error} />
          {receipts.data?.items.map((r) => <Received key={r.id} receipt={r} />)}
          {n.can_cancel && <Cancel notice={n} />}
        </>
      )}
    </>
  );
}
