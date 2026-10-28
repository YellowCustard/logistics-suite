import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { del, get, post } from "../../api/client";
import type { Webhook, WebhookCreated, WebhookDelivery, WebhookEvents, WebhookIn } from "../../api/types";
import { OneTime } from "../../components/Secret";
import { ErrorBox, Loading, Tag } from "../../components/ui";
import { DELIVERY_STATUS, when } from "../../format";

function Deliveries({ webhook }: { webhook: Webhook }) {
  const log = useQuery({
    queryKey: ["webhook-deliveries", webhook.id],
    queryFn: () => get<WebhookDelivery[]>(`/webhooks/${webhook.id}/deliveries?limit=50`),
    refetchInterval: 15_000, // retries and pings land in the background
  });
  if (log.isPending) return <Loading />;
  if (log.error) return <ErrorBox error={log.error} />;
  if (log.data.length === 0) return <p className="muted">Nothing sent yet.</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Event</th>
            <th scope="col">Status</th>
            <th scope="col">Attempts</th>
            <th scope="col">Last response</th>
            <th scope="col">Queued</th>
            <th scope="col">Delivered</th>
          </tr>
        </thead>
        <tbody>
          {log.data.map((d) => (
            <tr key={d.id}>
              <td className="mono">{d.event_type}</td>
              <td>
                <Tag value={d.status} labels={DELIVERY_STATUS} />
              </td>
              <td>{d.attempts}</td>
              <td className="wrap">{[d.last_status_code, d.last_error].filter(Boolean).join(" · ") || "—"}</td>
              <td>{when(d.created_at)}</td>
              <td>{when(d.delivered_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Subscription({ webhook }: { webhook: Webhook }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const ping = useMutation({
    mutationFn: () => post<WebhookDelivery>(`/webhooks/${webhook.id}/ping`),
    onSuccess: () => {
      setOpen(true);
      void queryClient.invalidateQueries({ queryKey: ["webhook-deliveries", webhook.id] });
    },
  });
  const disable = useMutation({
    mutationFn: () => del<Webhook>(`/webhooks/${webhook.id}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["webhooks"] }),
  });

  return (
    <div className="card webhook">
      <div className="title-row">
        <div>
          <div className="mono wrap">{webhook.url}</div>
          {webhook.description && <div className="muted">{webhook.description}</div>}
        </div>
        {webhook.is_active ? (
          <Tag value="good" labels={{ good: "Active" }} />
        ) : (
          <Tag value="cancelled" labels={{ cancelled: "Disabled" }} />
        )}
      </div>
      <p className="muted">Events: {webhook.event_types.join(", ")}</p>
      <div className="actions">
        <button type="button" className="secondary" onClick={() => setOpen((o) => !o)}>
          {open ? "Hide deliveries" : "Show deliveries"}
        </button>
        {webhook.is_active && (
          <>
            <button type="button" className="secondary" disabled={ping.isPending} onClick={() => ping.mutate()}>
              Send test event
            </button>
            <button
              type="button"
              className="secondary danger"
              disabled={disable.isPending}
              onClick={() => {
                if (window.confirm("Disable this webhook? Queued deliveries are dropped; add it again to restart."))
                  disable.mutate();
              }}
            >
              Disable
            </button>
          </>
        )}
      </div>
      <ErrorBox error={ping.error ?? disable.error} />
      {open && <Deliveries webhook={webhook} />}
    </div>
  );
}

function NewWebhook({ events, onCreated }: { events: string[]; onCreated: (w: WebhookCreated) => void }) {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState("https://");
  const [description, setDescription] = useState("");
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const create = useMutation({
    mutationFn: (body: WebhookIn) => post<WebhookCreated>("/webhooks", body),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ["webhooks"] });
      onCreated(created);
      setUrl("https://");
      setDescription("");
      setChosen(new Set());
    },
  });

  function toggle(event: string, on: boolean) {
    setChosen((current) => {
      const next = new Set(current);
      if (on) next.add(event);
      else next.delete(event);
      return next;
    });
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    create.mutate({ url: url.trim(), description: description.trim(), event_types: [...chosen] });
  }

  return (
    <form className="order-form" onSubmit={submit}>
      <fieldset>
        <legend>New webhook</legend>
        <div className="grid">
          <div>
            <label htmlFor="hook_url">HTTPS URL</label>
            <input id="hook_url" type="url" required maxLength={500} value={url} onChange={(e) => setUrl(e.target.value)} />
          </div>
          <div>
            <label htmlFor="hook_description">Description</label>
            <input
              id="hook_description"
              maxLength={200}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
        </div>
        <p className="muted">Events to send</p>
        <div className="checks">
          {events.map((event) => (
            <label key={event} className="check mono">
              <input type="checkbox" checked={chosen.has(event)} onChange={(e) => toggle(event, e.target.checked)} />{" "}
              {event}
            </label>
          ))}
        </div>
      </fieldset>
      <ErrorBox error={create.error} />
      <div className="actions">
        <button type="submit" disabled={create.isPending || chosen.size === 0}>
          Add webhook
        </button>
      </div>
    </form>
  );
}

export function Webhooks() {
  const hooks = useQuery({ queryKey: ["webhooks"], queryFn: () => get<Webhook[]>("/webhooks?include_inactive=true") });
  const events = useQuery({
    queryKey: ["webhook-events"],
    queryFn: () => get<WebhookEvents>("/webhooks/events"),
    staleTime: Infinity,
  });
  const [shown, setShown] = useState<WebhookCreated | null>(null);
  const active = hooks.data?.filter((h) => h.is_active) ?? [];
  const disabled = hooks.data?.filter((h) => !h.is_active) ?? [];

  return (
    <>
      <p className="muted">
        We POST a signed JSON event to your URL when something happens. Check the <code>Nuronteq-Signature</code>{" "}
        header (HMAC-SHA256 of <code>&lt;t&gt;.&lt;body&gt;</code> with the signing secret), reject old timestamps,
        and ignore repeats of an event id: delivery is at least once, retried with backoff on failure.
      </p>
      {shown && (
        <OneTime
          title={`Webhook added for ${shown.url}`}
          note="Copy the signing secret into the receiving system now. It is shown only this once."
          fields={[["Signing secret", shown.secret]]}
          onDone={() => setShown(null)}
        />
      )}
      <ErrorBox error={hooks.error ?? events.error} />
      {events.data && <NewWebhook events={events.data.event_types} onCreated={setShown} />}
      <h2>Webhooks</h2>
      {hooks.isPending ? (
        <Loading />
      ) : active.length === 0 && disabled.length === 0 ? (
        <p className="muted">No webhooks yet.</p>
      ) : (
        [...active, ...disabled].map((w) => <Subscription key={w.id} webhook={w} />)
      )}
    </>
  );
}
