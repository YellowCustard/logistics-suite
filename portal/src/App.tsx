import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router";

import { ApiError } from "./api/client";
import { AuthProvider, useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { Loading } from "./components/ui";
import { Shipments } from "./pages/Shipments";
import { SignIn } from "./pages/SignIn";
import { Tracking } from "./pages/Tracking";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      // Retry network blips, not answers: a 4xx won't change on a second try.
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
    },
  },
});

export const routes = [
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/shipments" replace /> },
      { path: "shipments", element: <Shipments /> },
      { path: "shipments/:id", element: <Tracking /> },
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
];

const router = createBrowserRouter(routes, { basename: "/portal" });

function Gate() {
  const { session, loading } = useAuth();
  if (loading) return <Loading />;
  if (!session) return <SignIn />;
  return <RouterProvider router={router} />;
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <Gate />
      </AuthProvider>
    </QueryClientProvider>
  );
}
