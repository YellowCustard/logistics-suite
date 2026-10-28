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
  return (
    <div className="flash bad" role="alert">
      {message}
    </div>
  );
}

export function Loading() {
  return <p className="muted">Loading…</p>;
}
