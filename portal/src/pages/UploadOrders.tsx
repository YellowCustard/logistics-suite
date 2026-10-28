import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router";

import { ApiError, upload } from "../api/client";
import { useItems, useSites } from "../api/lookups";
import type { OrderImport } from "../api/types";
import { downloadCsv, toCsv } from "../csv";
import { ErrorBox } from "../components/ui";

// The columns POST /orders:import accepts (OrderRow in apps/oms/api.py).
const COLUMNS = [
  "client_ref",
  "site_code",
  "ship_to_code",
  "ship_to_name",
  "ship_to_address",
  "required_by",
  "sku",
  "barcode",
  "qty",
  "uom",
  "contact_name",
  "contact_phone",
  "delivery_instructions",
  "notes",
];

function RowReport({ error }: { error: ApiError }) {
  return (
    <>
      <div className="flash bad" role="alert">
        {error.detail || error.title}
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Row</th>
              <th scope="col">Column</th>
              <th scope="col">Problem</th>
            </tr>
          </thead>
          <tbody>
            {error.errors.map((e, i) => (
              <tr key={i}>
                <td>{e.row ?? "—"}</td>
                <td className="mono">{e.field === "(row)" ? "whole order" : e.field || "—"}</td>
                <td className="wrap">{e.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export function UploadOrders() {
  const sites = useSites();
  const items = useItems();
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const send = useMutation({
    mutationFn: (csv: File) => {
      const form = new FormData();
      form.append("file", csv);
      return upload<OrderImport>("/orders:import", form);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
      void queryClient.invalidateQueries({ queryKey: ["order-counts"] });
    },
  });
  const rowErrors = send.error instanceof ApiError && send.error.errors.some((e) => e.row !== undefined);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (file) send.mutate(file);
  }

  function template() {
    const site = sites.data?.warehouses[0]?.code ?? "";
    const address = sites.data?.addresses[0]?.code ?? "";
    const skus = (items.data ?? []).slice(0, 2).map((i) => i.sku);
    const example = (sku: string) => ["PO-1001", site, address, "", "", "", sku, "", "1", "", "", "", "", ""];
    downloadCsv("orders-template.csv", toCsv(COLUMNS, skus.length ? skus.map(example) : [example("")]));
  }

  return (
    <>
      <p>
        <Link to="/orders">← Orders</Link>
      </p>
      <h1>Upload orders</h1>
      <p>
        One row per order line. Rows with the same <code>client_ref</code> make one order and must agree on
        everything except <code>sku</code>, <code>barcode</code>, <code>qty</code> and <code>uom</code>. Give a saved
        address as <code>ship_to_code</code>, or a new one as <code>ship_to_name</code> and{" "}
        <code>ship_to_address</code>. Dates are <code>YYYY-MM-DD</code>.
      </p>
      <p>
        If any row has a problem, nothing is imported and the problems are listed by row, so you can fix the file and
        upload it again.{" "}
        <button type="button" className="link" onClick={template}>
          Download a template
        </button>
      </p>
      <form className="filters" onSubmit={submit}>
        <div>
          <label htmlFor="file">CSV file (UTF-8)</label>
          <input
            id="file"
            type="file"
            accept=".csv,text/csv"
            required
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              send.reset();
            }}
          />
        </div>
        <button type="submit" disabled={!file || send.isPending}>
          {send.isPending ? "Uploading…" : "Upload"}
        </button>
      </form>
      {rowErrors ? <RowReport error={send.error as ApiError} /> : <ErrorBox error={send.error} />}
      {send.data && (
        <>
          <div className="flash ok" role="status">
            Imported {send.data.created} order{send.data.created === 1 ? "" : "s"}.
          </div>
          <ul>
            {send.data.orders.map((o) => (
              <li key={o.id}>
                <Link to={`/orders/${o.id}`}>{o.number}</Link> {o.client_ref && `(${o.client_ref})`}
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}
