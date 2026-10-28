import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router";

import { ApiError } from "./api/client";
import { AuthProvider, useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { Loading } from "./components/ui";
import { Documents } from "./pages/Documents";
import { Home } from "./pages/Home";
import { Inbound } from "./pages/Inbound";
import { InboundDetail } from "./pages/InboundDetail";
import { ItemDetail } from "./pages/ItemDetail";
import { Items } from "./pages/Items";
import { NewInbound } from "./pages/NewInbound";
import { NewItem } from "./pages/NewItem";
import { NewOrder } from "./pages/NewOrder";
import { OrderDetail } from "./pages/OrderDetail";
import { Orders } from "./pages/Orders";
import { Settings } from "./pages/Settings";
import { ApiCredentials } from "./pages/settings/ApiCredentials";
import { Users } from "./pages/settings/Users";
import { Webhooks } from "./pages/settings/Webhooks";
import { Shipments } from "./pages/Shipments";
import { SignIn } from "./pages/SignIn";
import { Stock } from "./pages/Stock";
import { Tracking } from "./pages/Tracking";
import { UploadInbound } from "./pages/UploadInbound";
import { UploadItems } from "./pages/UploadItems";
import { UploadOrders } from "./pages/UploadOrders";
import { Welcome } from "./pages/Welcome";

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
      { index: true, element: <Home /> },
      { path: "orders", element: <Orders /> },
      { path: "orders/new", element: <NewOrder /> },
      { path: "orders/upload", element: <UploadOrders /> },
      { path: "orders/:id", element: <OrderDetail /> },
      { path: "inbound", element: <Inbound /> },
      { path: "inbound/new", element: <NewInbound /> },
      { path: "inbound/upload", element: <UploadInbound /> },
      { path: "inbound/:id", element: <InboundDetail /> },
      { path: "stock", element: <Stock /> },
      { path: "shipments", element: <Shipments /> },
      { path: "shipments/:id", element: <Tracking /> },
      { path: "items", element: <Items /> },
      { path: "items/new", element: <NewItem /> },
      { path: "items/upload", element: <UploadItems /> },
      { path: "items/:id", element: <ItemDetail /> },
      { path: "documents", element: <Documents /> },
      {
        path: "settings",
        element: <Settings />,
        children: [
          { index: true, element: <Navigate to="/settings/users" replace /> },
          { path: "users", element: <Users /> },
          { path: "api", element: <ApiCredentials /> },
          { path: "webhooks", element: <Webhooks /> },
        ],
      },
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
];

const router = createBrowserRouter(routes, { basename: "/portal" });

function Gate() {
  const { session, loading } = useAuth();
  // A setup link works whoever is signed in on this browser: it sets the password of the user it was made for.
  if (window.location.pathname === "/portal/welcome") return <Welcome />;
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
