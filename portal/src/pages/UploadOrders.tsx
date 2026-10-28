import { Link } from "react-router";

import { useItems, useSites } from "../api/lookups";
import type { OrderImport } from "../api/types";
import { CsvUpload } from "../components/CsvUpload";

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

export function UploadOrders() {
  const sites = useSites();
  const items = useItems();

  function templateRows() {
    const site = sites.data?.warehouses[0]?.code ?? "";
    const address = sites.data?.addresses[0]?.code ?? "";
    const skus = (items.data ?? []).slice(0, 2).map((i) => i.sku);
    const example = (sku: string) => ["PO-1001", site, address, "", "", "", sku, "", "1", "", "", "", "", ""];
    return skus.length ? skus.map(example) : [example("")];
  }

  return (
    <>
      <p>
        <Link to="/orders">← Orders</Link>
      </p>
      <h1>Upload orders</h1>
      <CsvUpload
        endpoint="/orders:import"
        columns={COLUMNS}
        templateName="orders-template.csv"
        templateRows={templateRows}
        noun={["order", "orders"]}
        created={(result) => (result as OrderImport).orders}
        linkTo={(id) => `/orders/${id}`}
        invalidate={[["orders"], ["order-counts"]]}
      >
        <p>
          One row per order line. Rows with the same <code>client_ref</code> make one order and must agree on
          everything except <code>sku</code>, <code>barcode</code>, <code>qty</code> and <code>uom</code>. Give a saved
          address as <code>ship_to_code</code>, or a new one as <code>ship_to_name</code> and{" "}
          <code>ship_to_address</code>. Dates are <code>YYYY-MM-DD</code>.
        </p>
      </CsvUpload>
    </>
  );
}
