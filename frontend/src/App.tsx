import type { ReactNode } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router";

import { AuthProvider } from "./auth/AuthContext";
import { useAuth } from "./auth/context";
import {
  DashboardRoute,
  DashboardView,
} from "./routes/DashboardRoute";
import { LandingRoute } from "./routes/LandingRoute";
import { LoginRoute } from "./routes/LoginRoute";
import { RegisterRoute } from "./routes/RegisterRoute";

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}

function AppRoutes() {
  return (
    <Routes>
      <Route
        path="/login"
        element={
          <PublicOnlyRoute>
            <LoginRoute />
          </PublicOnlyRoute>
        }
      />
      <Route
        path="/register"
        element={
          <PublicOnlyRoute>
            <RegisterRoute />
          </PublicOnlyRoute>
        }
      />
      <Route path="/" element={<AdaptiveDashboardRoute />}>
        <Route index element={<DashboardView view="overview" />} />
        <Route path="repository" element={<DashboardView view="repository" />} />
        <Route path="files" element={<DashboardView view="files" />} />
        <Route path="commands" element={<DashboardView view="commands" />} />
        <Route path="agents" element={<DashboardView view="agent" />} />
        <Route path="editor" element={<DashboardView view="editor" />} />
        <Route path="terminal" element={<DashboardView view="terminal" />} />
        <Route path="preview" element={<DashboardView view="preview" />} />
        <Route path="audit" element={<DashboardView view="audit" />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function AdaptiveDashboardRoute() {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "loading") {
    return <FullPageMessage message="Loading Enigma..." />;
  }

  if (status === "unauthenticated") {
    return location.pathname === "/" ? (
      <LandingRoute />
    ) : (
      <Navigate to="/login" replace />
    );
  }

  return <DashboardRoute />;
}

function PublicOnlyRoute({ children }: { children: ReactNode }) {
  const { status } = useAuth();

  if (status === "loading") {
    return <FullPageMessage message="Loading session..." />;
  }

  if (status === "authenticated") {
    return <Navigate to="/" replace />;
  }

  return children;
}

function FullPageMessage({ message }: { message: string }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-canvas px-6 text-ink">
      <div className="rounded-xl border border-line bg-white px-5 py-4 shadow-soft">{message}</div>
    </div>
  );
}
