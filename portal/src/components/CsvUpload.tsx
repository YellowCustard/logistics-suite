import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router";

import { ApiError, upload } from "../api/client";
import { downloadCsv, toCsv } from "../csv";
import { ErrorBox } from "./ui";

type Created = { id: string; number: string; client_ref: string };

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
                <td className="mono">{e.field === "(row)" ? "whole record" : e.field || "—"}</td>
                <td className="wrap">{e.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

/** An all-or-nothing CSV import: upload, then either the problems by row or the records created. */
export function CsvUpload({
  endpoint,
  columns,
  templateName,
  templateRows,
  noun,
  created,
  linkTo,
  invalidate,
  children,
}: {
  endpoint: string;
  columns: string[];
  templateName: string;
  templateRows: () => unknown[][];
  noun: [string, string];
  created: (result: Record<string, unknown>) => Created[];
  linkTo: (id: string) => string;
  invalidate: string[][];
  children: ReactNode;
}) {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const send = useMutation({
    mutationFn: (csv: File) => {
      const form = new FormData();
      form.append("file", csv);
      return upload<Record<string, unknown>>(endpoint, form);
    },
    onSuccess: () => invalidate.forEach((queryKey) => void queryClient.invalidateQueries({ queryKey })),
  });
  const rowErrors = send.error instanceof ApiError && send.error.errors.some((e) => e.row !== undefined);
  const records = send.data ? created(send.data) : [];

  function submit(event: FormEvent) {
    event.preventDefault();
    if (file) send.mutate(file);
  }

  return (
    <>
      {children}
      <p>
        If any row has a problem, nothing is imported and the problems are listed by row, so you can fix the file and
        upload it again.{" "}
        <button type="button" className="link" onClick={() => downloadCsv(templateName, toCsv(columns, templateRows()))}>
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
            Imported {records.length} {records.length === 1 ? noun[0] : noun[1]}.
          </div>
          <ul>
            {records.map((r) => (
              <li key={r.id}>
                <Link to={linkTo(r.id)}>{r.number}</Link>{" "}
                {r.client_ref && `(${r.client_ref})`}
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}
