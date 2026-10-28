import type { ReactNode } from "react";

import { ApiError } from "../api/client";
import { label } from "../format";

export function Tag({ value, labels = {} }: { value: string; labels?: Record<string, string> }) {
  return <span className={`tag ${value}`}>{label(labels, value)}</span>;
}

export function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="card">
      <div className="card-label">{title}</div>
      <div className="card-value">{children}</div>
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const message =
    error instanceof ApiError
      ? error.status === 404
        ? "Not found. It may have been removed, or it belongs to another account."
        : error.detail || error.title
      : "Something went wrong. Check your connection and try again.";
  // Field-level problems from validation, e.g. "lines.0.qty: Input should be greater than 0".
  const fields = error instanceof ApiError ? error.errors.filter((e) => e.row === undefined).slice(0, 10) : [];
  return (
    <div className="flash bad" role="alert">
      {message}
      {fields.length > 0 && (
        <ul>
          {fields.map((e, i) => (
            <li key={i}>{e.field ? `${e.field.replace(/^body\./, "")}: ${e.message}` : e.message}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Loading() {
  return <p className="muted">Loading…</p>;
}
