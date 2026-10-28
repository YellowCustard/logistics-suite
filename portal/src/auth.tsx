import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

import { ApiError, del, get, onSignedOut, post } from "./api/client";
import type { Session } from "./api/types";

type Auth = {
  session: Session | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
};

const AuthContext = createContext<Auth | null>(null);

const isClient = (s: Session) => s.roles.some((r) => r === "client_admin" || r === "client_user");

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const queryClient = useQueryClient();

  useEffect(() => {
    // Makes sure the CSRF cookie exists (the Django page sets it too), then picks up an existing session.
    get("/auth/csrf")
      .catch(() => undefined)
      .then(() => get<Session>("/auth/session"))
      .then((s) => setSession(isClient(s) ? s : null))
      .catch(() => setSession(null))
      .finally(() => setLoading(false));
    return onSignedOut(() => {
      queryClient.clear(); // nothing from this session may show to whoever signs in next
      setSession(null);
    });
  }, [queryClient]);

  const signIn = useCallback(
    async (email: string, password: string) => {
      queryClient.clear();
      setSession(await post<Session>("/auth/session", { email, password, app: "portal" }));
    },
    [queryClient],
  );

  const signOut = useCallback(async () => {
    try {
      await del("/auth/session");
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
    }
    queryClient.clear();
    setSession(null);
  }, [queryClient]);

  return <AuthContext.Provider value={{ session, loading, signIn, signOut }}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth outside AuthProvider");
  return auth;
}
