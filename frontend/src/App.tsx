import type { ReactNode } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
} from "react-router";

import { AuthProvider } from "./auth/AuthContext";
import { useAuth } from "./auth/context";
import {
  DashboardRoute,
  DashboardView,
} from "./routes/DashboardRoute";
import { DashboardOverviewRoute } from "./routes/DashboardOverviewRoute";
import { LandingRoute } from "./routes/LandingRoute";
import { LoginRoute } from "./routes/LoginRoute";
import { ProjectListRoute } from "./routes/ProjectListRoute";
import { ProjectMembersRoute } from "./routes/ProjectMembersRoute";
import { ProjectRoute } from "./routes/ProjectRoute";
import { ProjectSettingsRoute } from "./routes/ProjectSettingsRoute";
import { RegisterRoute } from "./routes/RegisterRoute";
import { TeamMembersRoute } from "./routes/TeamMembersRoute";
import { TeamRoute } from "./routes/TeamRoute";

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

      {/* Project / team / workspace hierarchy */}
      <Route
        path="/projects"
        element={
          <PrivateRoute>
            <ProjectListRoute />
          </PrivateRoute>
        }
      />
      <Route
        path="/projects/:projectId"
        element={
          <PrivateRoute>
            <ProjectRoute />
          </PrivateRoute>
        }
      />
      <Route
        path="/projects/:projectId/settings"
        element={
          <PrivateRoute>
            <ProjectSettingsRoute />
          </PrivateRoute>
        }
      />
      <Route
        path="/projects/:projectId/members"
        element={
          <PrivateRoute>
            <ProjectMembersRoute />
          </PrivateRoute>
        }
      />
      <Route
        path="/projects/:projectId/teams/:teamId"
        element={
          <PrivateRoute>
            <TeamRoute />
          </PrivateRoute>
        }
      />
      <Route
        path="/projects/:projectId/teams/:teamId/members"
        element={
          <PrivateRoute>
            <TeamMembersRoute />
          </PrivateRoute>
        }
      />
      <Route
        path="/projects/:projectId/teams/:teamId/workspaces/:workspaceId"
        element={<AdaptiveDashboardRoute />}
      >
        <Route index element={<DashboardView view="overview" />} />
        <Route path="repository" element={<DashboardView view="repository" />} />
        <Route path="files" element={<DashboardView view="files" />} />
        <Route path="commands" element={<DashboardView view="commands" />} />
        <Route path="spec" element={<DashboardView view="spec" />} />
        <Route path="agents" element={<DashboardView view="agent" />} />
        <Route path="editor" element={<DashboardView view="editor" />} />
        <Route path="terminal" element={<DashboardView view="terminal" />} />
        <Route path="preview" element={<DashboardView view="preview" />} />
        <Route path="audit" element={<DashboardView view="audit" />} />
      </Route>

      {/* Home: projects-first. Authenticated users land on their project list. */}
      <Route path="/" element={<HomeRoute />} />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function HomeRoute() {
  const { status } = useAuth();

  if (status === "loading") {
    return <FullPageMessage message="Loading Enigma..." />;
  }

  if (status === "unauthenticated") {
    return <LandingRoute />;
  }

  return <DashboardOverviewRoute />;
}

function AdaptiveDashboardRoute() {
  const { status } = useAuth();

  if (status === "loading") {
    return <FullPageMessage message="Loading Enigma..." />;
  }

  if (status === "unauthenticated") {
    return <Navigate to="/login" replace />;
  }

  return <DashboardRoute />;
}

function PrivateRoute({ children }: { children: ReactNode }) {
  const { status } = useAuth();

  if (status === "loading") {
    return <FullPageMessage message="Loading session..." />;
  }

  if (status === "unauthenticated") {
    return <Navigate to="/login" replace />;
  }

  return children;
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
