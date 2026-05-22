import { type ReactNode, useEffect, useMemo, useState } from "react";

import { AuthContext, type AuthContextValue, type AuthStatus } from "./context";
import {
  getCurrentUser,
  login as loginRequest,
  logout as logoutRequest,
  register as registerRequest,
  type UserResponse,
} from "../lib/api";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<UserResponse | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    getCurrentUser(controller.signal)
      .then((currentUser) => {
        setUser(currentUser);
        setStatus("authenticated");
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setUser(null);
        setStatus("unauthenticated");
      });

    return () => controller.abort();
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      async login(payload) {
        const currentUser = await loginRequest(payload);
        setUser(currentUser);
        setStatus("authenticated");
      },
      async register(payload) {
        const currentUser = await registerRequest(payload);
        setUser(currentUser);
        setStatus("authenticated");
      },
      async logout() {
        try {
          await logoutRequest();
        } finally {
          setUser(null);
          setStatus("unauthenticated");
        }
      },
    }),
    [status, user],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
