import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { del, get, post } from "../../api/client";
import type { ApiCredential, ApiCredentialSecret } from "../../api/types";
import { OneTime } from "../../components/Secret";
import { ErrorBox, Loading, Tag } from "../../components/ui";
import { when } from "../../format";

function Row({ credential, onSecret }: { credential: ApiCredential; onSecret: (c: ApiCredentialSecret) => void }) {
  const queryClient = useQueryClient();
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["api-credentials"] });
  const rotate = useMutation({
    mutationFn: () => post<ApiCredentialSecret>(`/client/api-credentials/${credential.id}/rotate`),
    onSuccess: (result) => {
      refresh();
      onSecret(result);
    },
  });
  const revoke = useMutation({
    mutationFn: () => del<ApiCredential>(`/client/api-credentials/${credential.id}`),
    onSuccess: refresh,
  });

  return (
    <tr>
      <td>{credential.name}</td>
      <td className="mono">{credential.key_id}</td>
      <td>
        {credential.is_active ? (
          <Tag value="good" labels={{ good: "Active" }} />
        ) : (
          <Tag value="cancelled" labels={{ cancelled: "Revoked" }} />
        )}
      </td>
      <td>{when(credential.last_used_at)}</td>
      <td>
        {credential.is_active && (
          <div className="actions">
            <button
              type="button"
              className="secondary"
              disabled={rotate.isPending}
              onClick={() => {
                if (window.confirm(`Rotate ${credential.name}? The current secret stops working now.`)) rotate.mutate();
              }}
            >
              Rotate secret
            </button>
            <button
              type="button"
              className="secondary danger"
              disabled={revoke.isPending}
              onClick={() => {
                if (window.confirm(`Revoke ${credential.name}? Systems using it lose access at once, for good.`))
                  revoke.mutate();
              }}
            >
              Revoke
            </button>
          </div>
        )}
        <ErrorBox error={rotate.error ?? revoke.error} />
      </td>
    </tr>
  );
}

export function ApiCredentials() {
  const queryClient = useQueryClient();
  const credentials = useQuery({
    queryKey: ["api-credentials"],
    queryFn: () => get<ApiCredential[]>("/client/api-credentials"),
  });
  const [name, setName] = useState("");
  const [shown, setShown] = useState<ApiCredentialSecret | null>(null);
  const create = useMutation({
    mutationFn: () => post<ApiCredentialSecret>("/client/api-credentials", { name: name.trim() }),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["api-credentials"] });
      setShown(result);
      setName("");
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <>
      <p className="muted">
        Credentials let your own systems (ERP, WMS, e-commerce) use the API: exchange the client ID and secret for a
        token at <code>POST /api/v1/auth/token</code> (OAuth2 client credentials). Rotating replaces the secret; tokens
        already issued last until they expire, within an hour. Revoking cuts everything off at once.
      </p>
      {shown && (
        <OneTime
          title={`Secret for ${shown.name}`}
          note="Copy the secret into your system now. It is shown only this once; if it is lost, rotate it."
          fields={[
            ["Client ID", shown.key_id],
            ["Client secret", shown.secret],
          ]}
          onDone={() => setShown(null)}
        />
      )}
      <h2>New credential</h2>
      <form className="filters" onSubmit={submit}>
        <div>
          <label htmlFor="cred_name">Name, e.g. the system that will use it</label>
          <input id="cred_name" required maxLength={120} value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <button type="submit" disabled={create.isPending}>
          Create
        </button>
      </form>
      <ErrorBox error={create.error} />
      <h2>Credentials</h2>
      <ErrorBox error={credentials.error} />
      {credentials.isPending ? (
        <Loading />
      ) : credentials.data?.length === 0 ? (
        <p className="muted">No credentials yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Client ID</th>
                <th scope="col">Status</th>
                <th scope="col">Last token</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {credentials.data?.map((c) => (
                <Row key={c.id} credential={c} onSecret={setShown} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
