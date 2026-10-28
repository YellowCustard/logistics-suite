import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";

import { AuthContext } from "../auth";

/** Renders `element` at `path` (as a route pattern) with `url` as the current location; other routes listed in
 * `others` render their path as text, so a test can see where a navigation went. The signed-in user holds `roles`. */
export function renderAt(
  path: string,
  element: ReactElement,
  url: string,
  others: string[] = [],
  roles: string[] = ["client_user"],
) {
  const router = createMemoryRouter(
    [{ path, element }, ...others.map((p) => ({ path: p, element: <p>at {p}</p> }))],
    { initialEntries: [url] },
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const session = { user_id: "u1", email: "user@example.com", roles, all_sites: false, site_ids: [] };
  const auth = { session, loading: false, signIn: async () => {}, signOut: async () => {} };
  render(
    <QueryClientProvider client={client}>
      <AuthContext.Provider value={auth}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  );
  return router;
}
