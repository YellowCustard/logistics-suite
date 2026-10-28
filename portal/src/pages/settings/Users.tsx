import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { get, post, request } from "../../api/client";
import type { ClientUser, ClientUserIn, ClientUserInvited, ClientUserPatch, SetupLink } from "../../api/types";
import { useAuth } from "../../auth";
import { OneTime } from "../../components/Secret";
import { ErrorBox, Loading, Tag } from "../../components/ui";
import { CLIENT_ROLE, when } from "../../format";

const absolute = (link: string) => `${window.location.origin}${link}`;

type Shown = { email: string; link: string; reset: boolean };

function Invite({ onInvited }: { onInvited: (shown: Shown) => void }) {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<ClientUserIn["role"]>("client_user");
  const invite = useMutation({
    mutationFn: (body: ClientUserIn) => post<ClientUserInvited>("/client/users", body),
    onSuccess: (user) => {
      void queryClient.invalidateQueries({ queryKey: ["client-users"] });
      onInvited({ email: user.email, link: user.setup_link, reset: false });
      setEmail("");
      setName("");
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    invite.mutate({ email: email.trim(), full_name: name.trim(), role });
  }

  return (
    <form className="filters" onSubmit={submit}>
      <div>
        <label htmlFor="invite_email">Email</label>
        <input id="invite_email" type="email" required maxLength={254} value={email} onChange={(e) => setEmail(e.target.value)} />
      </div>
      <div>
        <label htmlFor="invite_name">Name</label>
        <input id="invite_name" maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div>
        <label htmlFor="invite_role">Role</label>
        <select id="invite_role" value={role} onChange={(e) => setRole(e.target.value as ClientUserIn["role"])}>
          <option value="client_user">User: orders, inbound, stock, tracking</option>
          <option value="client_admin">Administrator: also items and settings</option>
        </select>
      </div>
      <button type="submit" disabled={invite.isPending}>
        Add user
      </button>
      <ErrorBox error={invite.error} />
    </form>
  );
}

function Row({ user, me, onLink }: { user: ClientUser; me: boolean; onLink: (shown: Shown) => void }) {
  const queryClient = useQueryClient();
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["client-users"] });
  const change = useMutation({
    mutationFn: (body: ClientUserPatch) => request<ClientUser>("PATCH", `/client/users/${user.id}`, body),
    onSuccess: refresh,
  });
  const link = useMutation({
    mutationFn: () => post<SetupLink>(`/client/users/${user.id}/setup-link`),
    onSuccess: (result) => {
      refresh();
      onLink({ email: user.email, link: result.setup_link, reset: !user.pending });
    },
  });

  return (
    <tr>
      <td>
        {user.email}
        {user.full_name && <div className="muted">{user.full_name}</div>}
      </td>
      <td>
        {me ? (
          CLIENT_ROLE[user.role] ?? user.role
        ) : (
          <select
            aria-label={`Role of ${user.email}`}
            value={user.role}
            disabled={change.isPending}
            onChange={(e) => change.mutate({ role: e.target.value as ClientUserIn["role"] })}
          >
            {Object.entries(CLIENT_ROLE).map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </select>
        )}
      </td>
      <td>
        {!user.is_active ? (
          <Tag value="cancelled" labels={{ cancelled: "Inactive" }} />
        ) : user.pending ? (
          <Tag value="pending" labels={{ pending: "Link not used yet" }} />
        ) : (
          <Tag value="good" labels={{ good: "Active" }} />
        )}
      </td>
      <td>{when(user.last_login)}</td>
      <td>
        {me ? (
          <span className="muted">You</span>
        ) : (
          <div className="actions">
            {user.is_active && (
              <button
                type="button"
                className="secondary"
                disabled={link.isPending}
                onClick={() => {
                  if (
                    user.pending ||
                    window.confirm(`Reset ${user.email}'s password? They are signed out until they use the new link.`)
                  )
                    link.mutate();
                }}
              >
                {user.pending ? "New link" : "Reset password"}
              </button>
            )}
            <button
              type="button"
              className={user.is_active ? "secondary danger" : "secondary"}
              disabled={change.isPending}
              onClick={() => change.mutate({ is_active: !user.is_active })}
            >
              {user.is_active ? "Deactivate" : "Reactivate"}
            </button>
          </div>
        )}
        <ErrorBox error={change.error ?? link.error} />
      </td>
    </tr>
  );
}

export function Users() {
  const { session } = useAuth();
  const users = useQuery({ queryKey: ["client-users"], queryFn: () => get<ClientUser[]>("/client/users") });
  const [shown, setShown] = useState<Shown | null>(null);

  return (
    <>
      <p className="muted">
        People who sign in to this portal for your company. A new user gets a one-time link to choose their password:
        send it to them yourself. Links work once and expire after three days.
      </p>
      {shown && (
        <OneTime
          title={shown.reset ? `Password reset for ${shown.email}` : `${shown.email} can now set a password`}
          note="Send this link to them. It is shown only now and works once; you can make a new one at any time."
          fields={[["Setup link", absolute(shown.link)]]}
          onDone={() => setShown(null)}
        />
      )}
      <h2>Add a user</h2>
      <Invite onInvited={setShown} />
      <h2>Users</h2>
      <ErrorBox error={users.error} />
      {users.isPending ? (
        <Loading />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">User</th>
                <th scope="col">Role</th>
                <th scope="col">Status</th>
                <th scope="col">Last sign-in</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {users.data?.map((u) => (
                <Row key={u.id} user={u} me={u.id === session?.user_id} onLink={setShown} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
