import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";

import { post } from "../api/client";
import type { Item, ItemIn } from "../api/types";
import { ErrorBox } from "../components/ui";

const optional = (text: string) => (text.trim() === "" ? null : text.trim());

export function NewItem() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [form, setForm] = useState({
    sku: "",
    description: "",
    base_uom: "EA",
    case_uom: "",
    case_factor: "",
    units_per_pallet: "",
    weight_kg: "",
    volume_m3: "",
    barcode: "",
    lot_tracked: false,
    expiry_tracked: false,
  });
  const set = (change: Partial<typeof form>) => setForm((current) => ({ ...current, ...change }));

  const send = useMutation({
    mutationFn: (body: ItemIn) => post<Item>("/items", body),
    onSuccess: (item) => {
      void queryClient.invalidateQueries({ queryKey: ["items"] });
      navigate(`/items/${item.id}`);
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    send.mutate({
      sku: form.sku.trim(),
      description: form.description.trim(),
      base_uom: form.base_uom.trim() || "EA",
      case_uom: optional(form.case_uom),
      case_factor: form.case_factor ? Number(form.case_factor) : null,
      units_per_pallet: form.units_per_pallet ? Number(form.units_per_pallet) : null,
      weight_kg: optional(form.weight_kg),
      volume_m3: optional(form.volume_m3),
      barcode: optional(form.barcode),
      lot_tracked: form.lot_tracked,
      expiry_tracked: form.expiry_tracked,
    });
  }

  return (
    <>
      <p>
        <Link to="/items">← Items</Link>
      </p>
      <h1>New item</h1>
      <form className="order-form" onSubmit={submit}>
        <fieldset>
          <legend>Item</legend>
          <div className="grid">
            <div>
              <label htmlFor="sku">SKU</label>
              <input id="sku" required maxLength={60} value={form.sku} onChange={(e) => set({ sku: e.target.value })} />
            </div>
            <div>
              <label htmlFor="description">Description</label>
              <input
                id="description"
                required
                maxLength={255}
                value={form.description}
                onChange={(e) => set({ description: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="base_uom">Base unit</label>
              <input
                id="base_uom"
                required
                maxLength={10}
                value={form.base_uom}
                onChange={(e) => set({ base_uom: e.target.value.toUpperCase() })}
              />
            </div>
            <div>
              <label htmlFor="barcode">Barcode of the base unit (optional)</label>
              <input
                id="barcode"
                maxLength={64}
                autoComplete="off"
                value={form.barcode}
                onChange={(e) => set({ barcode: e.target.value })}
              />
            </div>
          </div>
        </fieldset>
        <fieldset>
          <legend>Packing and size</legend>
          <div className="grid">
            <div>
              <label htmlFor="case_uom">Case unit, e.g. CS (optional)</label>
              <input
                id="case_uom"
                maxLength={10}
                required={form.case_factor !== ""}
                value={form.case_uom}
                onChange={(e) => set({ case_uom: e.target.value.toUpperCase() })}
              />
            </div>
            <div>
              <label htmlFor="case_factor">Base units per case</label>
              <input
                id="case_factor"
                type="number"
                inputMode="numeric"
                min="2"
                step="1"
                required={form.case_uom.trim() !== ""}
                value={form.case_factor}
                onChange={(e) => set({ case_factor: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="units_per_pallet">Base units per pallet</label>
              <input
                id="units_per_pallet"
                type="number"
                inputMode="numeric"
                min="1"
                step="1"
                value={form.units_per_pallet}
                onChange={(e) => set({ units_per_pallet: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="weight_kg">Weight per base unit (kg)</label>
              <input
                id="weight_kg"
                type="number"
                inputMode="decimal"
                min="0"
                step="any"
                value={form.weight_kg}
                onChange={(e) => set({ weight_kg: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="volume_m3">Volume per base unit (m³)</label>
              <input
                id="volume_m3"
                type="number"
                inputMode="decimal"
                min="0"
                step="any"
                value={form.volume_m3}
                onChange={(e) => set({ volume_m3: e.target.value })}
              />
            </div>
          </div>
        </fieldset>
        <fieldset>
          <legend>Tracking</legend>
          <label className="check">
            <input
              type="checkbox"
              checked={form.lot_tracked}
              onChange={(e) => set({ lot_tracked: e.target.checked })}
            />{" "}
            Track lots (batch numbers)
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={form.expiry_tracked}
              onChange={(e) => set({ expiry_tracked: e.target.checked })}
            />{" "}
            Track expiry dates (stock is picked first-expiry-first-out)
          </label>
          <p className="muted">Tracking can't be changed after the item is created, so choose carefully.</p>
        </fieldset>
        <ErrorBox error={send.error} />
        <div className="actions">
          <button type="submit" disabled={send.isPending}>
            {send.isPending ? "Saving…" : "Create item"}
          </button>
        </div>
      </form>
    </>
  );
}
