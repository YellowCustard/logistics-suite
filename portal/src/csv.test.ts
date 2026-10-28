import { expect, it } from "vitest";

import { toCsv } from "./csv";

it("quotes what needs quoting and defuses formulas", () => {
  const csv = toCsv(["sku", "note", "qty"], [
    ["=HYPERLINK(1)", 'say "hi", twice', 12.5],
    ["-5", "line\nbreak", null],
    ["PLAIN", "", 0],
  ]);
  expect(csv.split("\r\n")).toEqual([
    "sku,note,qty",
    `'=HYPERLINK(1),"say ""hi"", twice",12.5`,
    `'-5,"line\nbreak",`,
    "PLAIN,,0",
    "",
  ]);
});
