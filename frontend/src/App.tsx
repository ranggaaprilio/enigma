import { useEffect, useState } from "react";

import { AuthProvider } from "./auth/AuthContext";
import { useAuth } from "./auth/context";
import { navigate } from "./lib/router";
import { DashboardRoute } from "./routes/DashboardRoute";
import { LoginRoute } from "./routes/LoginRoute";
import { RegisterRoute } from "./routes/RegisterRoute";

export default function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
}

function AppRoutes() {
  const pathname = usePathname();

  if (pathname === "/login") {
    return <LoginRoute />;
  }

  if (pathname === "/register") {
    return <RegisterRoute />;
  }

  return <ProtectedDashboard />;
}

function ProtectedDashboard() {
  const { status } = useAuth();

  useEffect(() => {
    if (status === "unauthenticated") {
      navigate("/login");
    }
  }, [status]);

  if (status === "loading") {
    return <FullPageMessage message="Loading session..." />;
  }

  if (status === "unauthenticated") {
    return <FullPageMessage message="Redirecting..." />;
  }

  return <DashboardRoute />;
}

function usePathname() {
  const [pathname, setPathname] = useState(window.location.pathname);

  useEffect(() => {
    const syncPathname = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", syncPathname);
    return () => window.removeEventListener("popstate", syncPathname);
  }, []);

  return pathname;
}

function FullPageMessage({ message }: { message: string }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-[#ece7dc] px-6 text-ink">
      <div className="rounded-md border border-line bg-[#fffdf8] px-5 py-4 shadow-soft">{message}</div>
    </div>
  );
}
