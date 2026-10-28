import { Link } from "react-router";

import type { ItemImport } from "../api/types";
import { CsvUpload } from "../components/CsvUpload";

// The columns POST /items:import accepts (ItemRow in apps/masterdata/imports.py).
const COLUMNS = [
  "sku",
  "description",
  "base_uom",
  "weight_kg",
  "volume_m3",
  "units_per_pallet",
  "lot_tracked",
  "expiry_tracked",
  "case_uom",
  "case_factor",
  "barcode",
];

const EXAMPLE = [
  ["WATER-500", "Still water 500ml", "EA", "0.52", "0.0006", "1440", "false", "false", "CS", "24", "6001234000013"],
  ["JUICE-1L", "Orange juice 1l", "EA", "1.05", "0.0011", "720", "true", "true", "CS", "6", ""],
];

export function UploadItems() {
  return (
    <>
      <p>
        <Link to="/items">← Items</Link>
      </p>
      <h1>Upload items</h1>
      <CsvUpload
        endpoint="/items:import"
        columns={COLUMNS}
        templateName="items-template.csv"
        templateRows={() => EXAMPLE}
        noun={["item", "items"]}
        created={(result) => (result as ItemImport).created}
        invalidate={[["items"]]}
      >
        <p>
          One row per new item; SKUs that already exist are rejected. Only <code>sku</code> and{" "}
          <code>description</code> are required. <code>base_uom</code> defaults to <code>EA</code>;{" "}
          <code>case_uom</code> and <code>case_factor</code> go together; <code>lot_tracked</code> and{" "}
          <code>expiry_tracked</code> are <code>true</code> or <code>false</code>. A barcode the length of an EAN or
          GTIN must have the right check digit.
        </p>
      </CsvUpload>
    </>
  );
}
