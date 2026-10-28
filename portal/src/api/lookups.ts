import { useQuery } from "@tanstack/react-query";

import { get, query } from "./client";
import type { Item, Site, StockSummary } from "./types";

const MAX_PAGES = 50; // 10 000 rows; a client catalogue is far smaller

/** Follows next_cursor to the end. For small reference lists only. */
async function all<T>(path: string, params: Record<string, string> = {}): Promise<T[]> {
  const rows: T[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < MAX_PAGES; page++) {
    const result: { items: T[]; next_cursor: string | null } = await get(
      `${path}${query({ ...params, limit: 200, cursor })}`,
    );
    rows.push(...result.items);
    cursor = result.next_cursor;
    if (!cursor) break;
  }
  return rows;
}

const byCode = (a: Site, b: Site) => a.code.localeCompare(b.code);

/** Warehouses the client ships from and its own delivery addresses. */
export function useSites() {
  return useQuery({
    queryKey: ["sites"],
    queryFn: async () => {
      const sites = await all<Site>("/sites");
      return {
        all: sites,
        warehouses: sites.filter((s) => s.kind === "warehouse").sort(byCode),
        addresses: sites.filter((s) => s.kind === "delivery_address").sort((a, b) => a.name.localeCompare(b.name)),
        byId: new Map(sites.map((s) => [s.id, s])),
      };
    },
    staleTime: 5 * 60_000,
  });
}

export function useItems() {
  return useQuery({
    queryKey: ["items"],
    queryFn: async () => (await all<Item>("/items")).sort((a, b) => a.sku.localeCompare(b.sku)),
    staleTime: 5 * 60_000,
  });
}

/** The warehouse a new order or notice most likely concerns: where the client holds the most stock lines,
 * else the first warehouse. Shares its cache with the Stock and Home screens. */
export function usePreferredWarehouse(): string {
  const sites = useSites();
  const stock = useQuery({
    queryKey: ["stock", "", ""],
    queryFn: () => get<StockSummary>("/inventory/summary"),
  });
  const lines = new Map<string, number>();
  for (const row of stock.data?.items ?? []) lines.set(row.site_code, (lines.get(row.site_code) ?? 0) + 1);
  const busiest = [...lines.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
  return busiest ?? sites.data?.warehouses[0]?.code ?? "";
}
