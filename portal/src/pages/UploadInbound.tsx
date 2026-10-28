import { Link } from "react-router";

import { useItems, useSites } from "../api/lookups";
import type { NoticeImport } from "../api/types";
import { CsvUpload } from "../components/CsvUpload";

// The columns POST /inbound-notices:import accepts (AsnRow in apps/oms/api.py).
const COLUMNS = ["client_ref", "site_code", "expected_at", "sku", "barcode", "qty", "uom", "lot", "expiry", "kind", "notes"];

export function UploadInbound() {
  const sites = useSites();
  const items = useItems();

  function templateRows() {
    const site = sites.data?.warehouses[0]?.code ?? "";
    const skus = (items.data ?? []).slice(0, 2).map((i) => i.sku);
    const example = (sku: string) => ["ASN-1001", site, "2026-11-02T08:00:00+02:00", sku, "", "1", "", "", "", "purchase", ""];
    return skus.length ? skus.map(example) : [example("")];
  }

  return (
    <>
      <p>
        <Link to="/inbound">← Inbound</Link>
      </p>
      <h1>Upload inbound notices</h1>
      <CsvUpload
        endpoint="/inbound-notices:import"
        columns={COLUMNS}
        templateName="inbound-template.csv"
        templateRows={templateRows}
        noun={["notice", "notices"]}
        created={(result) => (result as NoticeImport).notices}
        linkTo={(id) => `/inbound/${id}`}
        invalidate={[["notices"]]}
      >
        <p>
          One row per line. Rows with the same <code>client_ref</code> make one notice and must share{" "}
          <code>site_code</code>, <code>expected_at</code>, <code>kind</code> and <code>notes</code>. Times are ISO 8601
          with the zone, for example <code>2026-11-02T08:00:00+02:00</code>; dates are <code>YYYY-MM-DD</code>.{" "}
          <code>kind</code> is <code>purchase</code> (the default) or <code>return</code>.
        </p>
      </CsvUpload>
    </>
  );
}
