import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router";

import { ApiError, del, get, patch, post } from "../api/client";
import type { Item, ItemPatch } from "../api/types";
import { useIsClientAdmin } from "../auth";
import { Card, ErrorBox, Loading } from "../components/ui";
import { qty } from "../format";

/** Saves the item in the cache the screens read from, so every list shows the change at once. */
function useItemSaved(id: string) {
  const queryClient = useQueryClient();
  return (item: Item) => {
    queryClient.setQueryData(["item", id], item);
    void queryClient.invalidateQueries({ queryKey: ["items"] });
  };
}

/** A 412 means someone else saved first: say so and load their version. */
function useReloadOnConflict(id: string) {
  const queryClient = useQueryClient();
  return (error: Error) => {
    if (error instanceof ApiError && error.status === 412) void queryClient.invalidateQueries({ queryKey: ["item", id] });
  };
}

const decimalOrNull = (text: string) => (text.trim() === "" ? null : text.trim());

/** Remounted (by key) whenever the item's version changes, so it always edits the latest values; the parent
 * remembers which version this form saved, to say so after the remount. */
function Details({ item, saved, onSaved }: { item: Item; saved: boolean; onSaved: (item: Item) => void }) {
  const store = useItemSaved(item.id);
  const [description, setDescription] = useState(item.description);
  const [weight, setWeight] = useState(item.weight_kg ?? "");
  const [volume, setVolume] = useState(item.volume_m3 ?? "");
  const [perPallet, setPerPallet] = useState(item.units_per_pallet?.toString() ?? "");
  const save = useMutation({
    mutationFn: (body: ItemPatch) => patch<Item>(`/items/${item.id}`, body, item.version),
    onSuccess: (updated) => {
      onSaved(updated);
      store(updated);
    },
    onError: useReloadOnConflict(item.id),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate({
      description: description.trim(),
      weight_kg: decimalOrNull(weight),
      volume_m3: decimalOrNull(volume),
      units_per_pallet: perPallet.trim() === "" ? null : Number(perPallet),
    });
  }

  return (
    <form className="order-form" onSubmit={submit}>
      <fieldset>
        <legend>Details</legend>
        <div className="grid">
          <div>
            <label htmlFor="description">Description</label>
            <input
              id="description"
              required
              maxLength={255}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="weight">Weight per {item.base_uom} (kg)</label>
            <input
              id="weight"
              type="number"
              inputMode="decimal"
              min="0"
              step="any"
              value={weight}
              onChange={(e) => setWeight(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="volume">Volume per {item.base_uom} (m³)</label>
            <input
              id="volume"
              type="number"
              inputMode="decimal"
              min="0"
              step="any"
              value={volume}
              onChange={(e) => setVolume(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="per_pallet">{item.base_uom} per pallet</label>
            <input
              id="per_pallet"
              type="number"
              inputMode="numeric"
              min="1"
              step="1"
              value={perPallet}
              onChange={(e) => setPerPallet(e.target.value)}
            />
          </div>
        </div>
      </fieldset>
      <ErrorBox error={save.error} />
      {saved && (
        <div className="flash ok" role="status">
          Saved.
        </div>
      )}
      <div className="actions">
        <button type="submit" disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

function PackSizes({ item, admin }: { item: Item; admin: boolean }) {
  const saved = useItemSaved(item.id);
  const [uom, setUom] = useState("");
  const [factor, setFactor] = useState("");
  const add = useMutation({
    mutationFn: () => post<Item>(`/items/${item.id}/pack-sizes`, { uom: uom.trim(), factor: Number(factor) }),
    onSuccess: (updated) => {
      saved(updated);
      setUom("");
      setFactor("");
    },
  });

  return (
    <section>
      <h2>Pack sizes</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Unit</th>
              <th scope="col">Contains</th>
            </tr>
          </thead>
          <tbody>
            {item.pack_sizes.map((p) => (
              <tr key={p.uom}>
                <td className="mono">{p.uom}</td>
                <td>{p.factor === 1 ? "Base unit" : `${qty(p.factor)} × ${item.base_uom}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {admin && (
        <form
          className="filters"
          onSubmit={(e) => {
            e.preventDefault();
            add.mutate();
          }}
        >
          <div>
            <label htmlFor="pack_uom">New unit, e.g. CS</label>
            <input
              id="pack_uom"
              required
              maxLength={10}
              pattern="[A-Za-z0-9]{1,10}"
              title="Letters and digits, up to 10"
              value={uom}
              onChange={(e) => setUom(e.target.value.toUpperCase())}
            />
          </div>
          <div>
            <label htmlFor="pack_factor">Number of {item.base_uom} in it</label>
            <input
              id="pack_factor"
              type="number"
              inputMode="numeric"
              required
              min="2"
              step="1"
              value={factor}
              onChange={(e) => setFactor(e.target.value)}
            />
          </div>
          <button type="submit" className="secondary" disabled={add.isPending}>
            Add pack size
          </button>
        </form>
      )}
      <ErrorBox error={add.error} />
      {admin && (
        <p className="muted">
          A pack size can't be changed or removed once added, because orders and stock may already use it. Ask the
          warehouse if one is wrong.
        </p>
      )}
    </section>
  );
}

function Barcodes({ item, admin }: { item: Item; admin: boolean }) {
  const saved = useItemSaved(item.id);
  const [code, setCode] = useState("");
  const [uom, setUom] = useState(item.base_uom);
  const add = useMutation({
    mutationFn: () => post<Item>(`/items/${item.id}/barcodes`, { barcode: code.trim(), uom }),
    onSuccess: (updated) => {
      saved(updated);
      setCode("");
    },
  });
  const remove = useMutation({
    mutationFn: (barcodeId: string) => del<Item>(`/items/${item.id}/barcodes/${barcodeId}`),
    onSuccess: saved,
  });

  return (
    <section>
      <h2>Barcodes</h2>
      {item.barcodes.length === 0 ? (
        <p className="muted">No barcodes. Scanners will find this item by its SKU only.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Barcode</th>
                <th scope="col">Unit it identifies</th>
                {admin && (
                  <th scope="col">
                    <span className="sr-only">Remove</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {item.barcodes.map((b) => (
                <tr key={b.id}>
                  <td className="mono">{b.barcode}</td>
                  <td>{b.uom}</td>
                  {admin && (
                    <td>
                      <button
                        type="button"
                        className="secondary"
                        disabled={remove.isPending}
                        aria-label={`Remove barcode ${b.barcode}`}
                        onClick={() => {
                          if (window.confirm(`Remove barcode ${b.barcode}? Scans of it will no longer find ${item.sku}.`))
                            remove.mutate(b.id);
                        }}
                      >
                        Remove
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <ErrorBox error={remove.error} />
      {admin && (
        <form
          className="filters"
          onSubmit={(e) => {
            e.preventDefault();
            add.mutate();
          }}
        >
          <div>
            <label htmlFor="barcode">New barcode (EAN, UPC, GTIN-14 or your own code)</label>
            <input
              id="barcode"
              required
              maxLength={64}
              autoComplete="off"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="barcode_uom">Unit</label>
            <select id="barcode_uom" value={uom} onChange={(e) => setUom(e.target.value)}>
              {item.pack_sizes.map((p) => (
                <option key={p.uom} value={p.uom}>
                  {p.uom}
                </option>
              ))}
            </select>
          </div>
          <button type="submit" className="secondary" disabled={add.isPending}>
            Add barcode
          </button>
        </form>
      )}
      <ErrorBox error={add.error} />
    </section>
  );
}

function Status({ item }: { item: Item }) {
  const saved = useItemSaved(item.id);
  const toggle = useMutation({
    mutationFn: () => patch<Item>(`/items/${item.id}`, { is_active: !item.is_active }, item.version),
    onSuccess: saved,
    onError: useReloadOnConflict(item.id),
  });
  return (
    <section>
      <h2>{item.is_active ? "Stop using this item" : "Use this item again"}</h2>
      <p className="muted">
        {item.is_active
          ? "An inactive item can't be ordered or sent in. Stock already in the warehouse is not affected."
          : "This item is inactive: new orders and inbound notices can't use it."}
      </p>
      <ErrorBox error={toggle.error} />
      <div className="actions">
        <button
          type="button"
          className={item.is_active ? "secondary danger" : "secondary"}
          disabled={toggle.isPending}
          onClick={() => toggle.mutate()}
        >
          {item.is_active ? "Make inactive" : "Make active"}
        </button>
      </div>
    </section>
  );
}

export function ItemDetail() {
  const { id = "" } = useParams();
  const admin = useIsClientAdmin();
  const item = useQuery({ queryKey: ["item", id], queryFn: () => get<Item>(`/items/${id}`) });
  const [savedVersion, setSavedVersion] = useState<number | null>(null);

  if (item.isPending) return <Loading />;
  if (!item.data) return <ErrorBox error={item.error} />;
  const i = item.data;
  return (
    <>
      <p>
        <Link to="/items">← Items</Link>
      </p>
      <div className="title-row">
        <h1>
          <span className="mono">{i.sku}</span> {i.description}
        </h1>
      </div>
      <div className="cards">
        <Card title="Base unit">{i.base_uom}</Card>
        <Card title="Tracked by">
          {[i.lot_tracked && "Lot", i.expiry_tracked && "Expiry"].filter(Boolean).join(", ") || "Nothing"}
        </Card>
        <Card title="Status">{i.is_active ? "Active" : "Inactive"}</Card>
      </div>
      <ErrorBox error={item.error} />
      {admin ? (
        <Details
          key={i.version}
          item={i}
          saved={savedVersion === i.version}
          onSaved={(updated) => setSavedVersion(updated.version)}
        />
      ) : (
        <p className="muted">
          Weight {i.weight_kg ? `${qty(i.weight_kg)} kg` : "not set"}, volume {i.volume_m3 ? `${i.volume_m3} m³` : "not set"},{" "}
          {i.units_per_pallet ? `${qty(i.units_per_pallet)} ${i.base_uom} per pallet` : "pallet quantity not set"}. Your
          account administrator can change items.
        </p>
      )}
      <PackSizes item={i} admin={admin} />
      <Barcodes item={i} admin={admin} />
      {admin && <Status item={i} />}
      <p className="muted">SKU, base unit and lot or expiry tracking can't be changed here; ask the warehouse.</p>
    </>
  );
}
