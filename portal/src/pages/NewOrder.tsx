import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";

import { post } from "../api/client";
import { useItems, useSites } from "../api/lookups";
import type { Order, OrderIn } from "../api/types";
import { ErrorBox, Loading } from "../components/ui";

type Line = { key: number; sku: string; qty: string; uom: string };

let nextKey = 1;
const blankLine = (): Line => ({ key: nextKey++, sku: "", qty: "", uom: "" });

export function NewOrder() {
  const sites = useSites();
  const items = useItems();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [siteCode, setSiteCode] = useState("");
  const [shipMode, setShipMode] = useState<"saved" | "new">("saved");
  const [shipToCode, setShipToCode] = useState("");
  const [shipName, setShipName] = useState("");
  const [shipAddress, setShipAddress] = useState("");
  const [clientRef, setClientRef] = useState("");
  const [requiredBy, setRequiredBy] = useState("");
  const [contactName, setContactName] = useState("");
  const [contactPhone, setContactPhone] = useState("");
  const [instructions, setInstructions] = useState("");
  const [notes, setNotes] = useState("");
  const [lines, setLines] = useState<Line[]>([blankLine()]);

  const known = new Set((items.data ?? []).map((i) => i.sku));
  const unknown = lines.filter((l) => l.sku.trim() && items.data && !known.has(l.sku.trim())).map((l) => l.sku.trim());
  const addresses = sites.data?.addresses ?? [];
  const mode = addresses.length === 0 ? "new" : shipMode;

  const place = useMutation({
    mutationFn: (body: OrderIn) => post<Order>("/orders", body),
    onSuccess: (order) => {
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
      void queryClient.invalidateQueries({ queryKey: ["order-counts"] });
      void queryClient.invalidateQueries({ queryKey: ["sites"] }); // a new address is saved for next time
      navigate(`/orders/${order.id}`);
    },
  });

  function setLine(key: number, change: Partial<Line>) {
    setLines((current) => current.map((l) => (l.key === key ? { ...l, ...change } : l)));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    place.mutate({
      site_code: siteCode || sites.data?.warehouses[0]?.code || "",
      client_ref: clientRef.trim(),
      ship_to_code: mode === "saved" ? shipToCode || addresses[0]?.code || "" : "",
      ship_to: mode === "new" ? { name: shipName.trim(), address: shipAddress.trim() } : null,
      required_by: requiredBy || null,
      contact_name: contactName.trim(),
      contact_phone: contactPhone.trim(),
      delivery_instructions: instructions.trim(),
      notes: notes.trim(),
      lines: lines.map((l) => ({ sku: l.sku.trim(), qty: l.qty, uom: l.uom.trim() })),
    });
  }

  if (sites.isPending) return <Loading />;
  return (
    <>
      <p>
        <Link to="/orders">← Orders</Link>
      </p>
      <h1>New order</h1>
      <ErrorBox error={sites.error ?? items.error} />
      <form className="order-form" onSubmit={submit}>
        <fieldset>
          <legend>Order</legend>
          <div className="grid">
            <div>
              <label htmlFor="site">Ship from</label>
              <select id="site" required value={siteCode} onChange={(e) => setSiteCode(e.target.value)}>
                {sites.data?.warehouses.map((s) => (
                  <option key={s.id} value={s.code}>
                    {s.name} ({s.code})
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="client_ref">Your reference</label>
              <input id="client_ref" maxLength={60} value={clientRef} onChange={(e) => setClientRef(e.target.value)} />
            </div>
            <div>
              <label htmlFor="required_by">Required by</label>
              <input id="required_by" type="date" value={requiredBy} onChange={(e) => setRequiredBy(e.target.value)} />
            </div>
          </div>
        </fieldset>

        <fieldset>
          <legend>Deliver to</legend>
          {addresses.length > 0 && (
            <div className="choice">
              <label>
                <input type="radio" checked={mode === "saved"} onChange={() => setShipMode("saved")} /> A saved address
              </label>
              <label>
                <input type="radio" checked={mode === "new"} onChange={() => setShipMode("new")} /> A new address
              </label>
            </div>
          )}
          {mode === "saved" ? (
            <>
              <label htmlFor="ship_to">Address</label>
              <select id="ship_to" value={shipToCode} onChange={(e) => setShipToCode(e.target.value)}>
                {addresses.map((s) => (
                  <option key={s.id} value={s.code}>
                    {s.name}, {s.address}
                  </option>
                ))}
              </select>
            </>
          ) : (
            <div className="grid">
              <div>
                <label htmlFor="ship_name">Name</label>
                <input id="ship_name" required maxLength={120} value={shipName} onChange={(e) => setShipName(e.target.value)} />
              </div>
              <div>
                <label htmlFor="ship_address">Street address</label>
                <textarea
                  id="ship_address"
                  required
                  rows={2}
                  value={shipAddress}
                  onChange={(e) => setShipAddress(e.target.value)}
                />
              </div>
            </div>
          )}
          <div className="grid">
            <div>
              <label htmlFor="contact_name">Contact person</label>
              <input id="contact_name" maxLength={120} value={contactName} onChange={(e) => setContactName(e.target.value)} />
            </div>
            <div>
              <label htmlFor="contact_phone">Contact phone</label>
              <input
                id="contact_phone"
                type="tel"
                maxLength={30}
                value={contactPhone}
                onChange={(e) => setContactPhone(e.target.value)}
              />
            </div>
          </div>
          <label htmlFor="instructions">Delivery instructions</label>
          <textarea id="instructions" rows={2} value={instructions} onChange={(e) => setInstructions(e.target.value)} />
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
                  <th scope="col">
                    <span className="sr-only">Remove</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {lines.map((line, index) => (
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
                        placeholder="Base unit"
                        value={line.uom}
                        onChange={(e) => setLine(line.key, { uom: e.target.value })}
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
                ))}
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

        <ErrorBox error={place.error} />
        <div className="actions">
          <button type="submit" disabled={place.isPending}>
            {place.isPending ? "Placing order…" : "Place order"}
          </button>
        </div>
      </form>
    </>
  );
}
