import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";

/** Renders `element` at `path` (as a route pattern) with `url` as the current location; other routes listed in
 * `others` render their path as text, so a test can see where a navigation went. */
export function renderAt(path: string, element: ReactElement, url: string, others: string[] = []) {
  const router = createMemoryRouter(
    [{ path, element }, ...others.map((p) => ({ path: p, element: <p>at {p}</p> }))],
    { initialEntries: [url] },
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}
