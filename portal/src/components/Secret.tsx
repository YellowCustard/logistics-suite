import { useState } from "react";

function CopyField({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const id = `secret-${label.toLowerCase().replace(/\W+/g, "-")}`;
  return (
    <div className="copy-field">
      <label htmlFor={id}>{label}</label>
      <div>
        <input id={id} className="mono" readOnly value={value} onFocus={(e) => e.target.select()} />
        <button
          type="button"
          className="secondary"
          onClick={() =>
            void navigator.clipboard?.writeText(value).then(
              () => setCopied(true),
              () => setCopied(false), // the field stays selectable for a manual copy
            )
          }
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

/** Something the server shows only once (a secret or a setup link). It lives in this component's props only:
 * nothing stores it, and it is gone when the screen changes. */
export function OneTime({
  title,
  fields,
  note,
  onDone,
}: {
  title: string;
  fields: [string, string][];
  note: string;
  onDone: () => void;
}) {
  return (
    <div className="flash ok one-time" role="status">
      <strong>{title}</strong>
      <p>{note}</p>
      {fields.map(([label, value]) => (
        <CopyField key={label} label={label} value={value} />
      ))}
      <div className="actions">
        <button type="button" onClick={onDone}>
          Done
        </button>
      </div>
    </div>
  );
}
