import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router";

import { documentUrl, get } from "../api/client";
import type { DocumentRef, Pod, Tracking as TrackingData } from "../api/types";
import { Card, ErrorBox, Loading, Tag } from "../components/ui";
import { day, FAILURE_REASON, label, SHIPMENT_STATUS, TRACKING_EVENT, when } from "../format";

const REFRESH_MS = 60_000; // ETAs move as the vehicle reports; refresh while the page is open

function progress(t: TrackingData): string {
  if (t.status !== "in_transit" || t.stops_before === null || t.stops_before === undefined) return "—";
  if (t.stops_before === 0) return "Next stop";
  return `${t.stops_before} stop${t.stops_before === 1 ? "" : "s"} before yours`;
}

function Evidence({ doc, caption }: { doc: DocumentRef; caption: string }) {
  const url = documentUrl(doc.id);
  return (
    <figure>
      <a href={url}>
        <img src={url} alt={caption} />
      </a>
      <figcaption>
        {caption} · <a href={url}>Download</a>
      </figcaption>
    </figure>
  );
}

function ProofOfDelivery({ pod }: { pod: Pod | null | undefined }) {
  if (!pod) {
    return (
      <>
        <h2>Proof of delivery</h2>
        <p className="muted">Not delivered yet.</p>
      </>
    );
  }
  const delivered = pod.outcome === "delivered";
  return (
    <>
      <h2>{delivered ? "Proof of delivery" : "Delivery attempt"}</h2>
      <div className="cards">
        <Card title="Outcome">
          <Tag value={pod.outcome} />
        </Card>
        <Card title={delivered ? "Received by" : "Reason"}>
          {delivered ? pod.recipient_name : label(FAILURE_REASON, pod.reason)}
        </Card>
        <Card title="Captured">{when(pod.captured_at)}</Card>
        <Card title="Trip">{pod.trip_number}</Card>
      </div>
      {pod.note && (
        <p>
          <strong>Driver's note:</strong> {pod.note}
        </p>
      )}
      {(pod.signature || pod.photos.length > 0) && (
        <div className="evidence">
          {pod.signature && <Evidence doc={pod.signature} caption="Signature" />}
          {pod.photos.map((photo, i) => (
            <Evidence key={photo.id} doc={photo} caption={`Photo ${i + 1}`} />
          ))}
        </div>
      )}
      {pod.units.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">SSCC</th>
                <th scope="col">Kind</th>
                <th scope="col">Cartons</th>
                <th scope="col">Outcome</th>
              </tr>
            </thead>
            <tbody>
              {pod.units.map((u) => (
                <tr key={u.sscc}>
                  <td className="mono">{u.sscc}</td>
                  <td>{u.kind}</td>
                  <td>{u.cartons}</td>
                  <td>
                    <Tag value={u.delivery_status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export function Tracking() {
  const { id = "" } = useParams();
  const tracking = useQuery({
    queryKey: ["tracking", id],
    queryFn: () => get<TrackingData>(`/shipments/${encodeURIComponent(id)}/tracking`),
    refetchInterval: (q) => (q.state.data?.status === "in_transit" ? REFRESH_MS : false),
  });

  return (
    <>
      <p>
        <Link to="/shipments">← Tracking</Link>
      </p>
      <ErrorBox error={tracking.error} />
      {tracking.isPending && <Loading />}
      {tracking.data && (
        <>
          <h1>Shipment {tracking.data.number}</h1>
          <div className="cards">
            <Card title="Status">
              <Tag value={tracking.data.status} labels={SHIPMENT_STATUS} />
            </Card>
            <Card title="Deliver to">{tracking.data.ship_to_name}</Card>
            <Card title="Your reference">{tracking.data.client_ref || tracking.data.order_number}</Card>
            <Card title="Required by">{day(tracking.data.required_by)}</Card>
            <Card title="Estimated arrival">{when(tracking.data.eta)}</Card>
            <Card title="Progress">{progress(tracking.data)}</Card>
          </div>
          <h2>Timeline</h2>
          <ol className="timeline">
            {tracking.data.events.map((e, i) => (
              <li key={`${e.what}-${i}`}>
                <div>
                  {label(TRACKING_EVENT, e.what)}
                  {e.what === "delivery_failed" && e.detail ? ` (${label(FAILURE_REASON, e.detail)})` : ""}
                </div>
                <div className="when">{when(e.at)}</div>
              </li>
            ))}
          </ol>
          <ProofOfDelivery pod={tracking.data.pod} />
        </>
      )}
    </>
  );
}
