import { useQuery } from "@tanstack/react-query";

import { get, query } from "./client";
import type { Item, Site } from "./types";

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
