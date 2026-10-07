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
import { LlmStatusProvider } from "./platform/LlmStatusProvider";
import { useLlmSetupSkipped, useLlmStatus } from "./platform/llmStatus";
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
import { SettingsRoute } from "./routes/SettingsRoute";
import { SetupRoute } from "./routes/SetupRoute";
import { TeamMembersRoute } from "./routes/TeamMembersRoute";
import { TeamRoute } from "./routes/TeamRoute";

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <LlmStatusProvider>
          <AppRoutes />
        </LlmStatusProvider>
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
        <Route path="usage" element={<DashboardView view="usage" />} />
        <Route path="editor" element={<DashboardView view="editor" />} />
        <Route path="terminal" element={<DashboardView view="terminal" />} />
        <Route path="preview" element={<DashboardView view="preview" />} />
        <Route path="audit" element={<DashboardView view="audit" />} />
      </Route>

      {/* Platform-wide settings. Any authenticated user may look; only the platform owner
          may edit (the page renders a locked state for everyone else). */}
      <Route
        path="/settings"
        element={
          <PrivateRoute>
            <SettingsRoute />
          </PrivateRoute>
        }
      />
      {/* First-run LLM setup. Wrapped in `RequireAuth`, deliberately NOT `PrivateRoute`:
          `PrivateRoute` carries the setup gate, and gating the setup page is a redirect loop. */}
      <Route
        path="/setup"
        element={
          <RequireAuth>
            <SetupRoute />
          </RequireAuth>
        }
      />

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

  // `HomeRoute` hand-rolls its auth check instead of using `PrivateRoute`, so the setup gate
  // has to be applied explicitly here or `/` bypasses it entirely.
  return (
    <SetupGate>
      <DashboardOverviewRoute />
    </SetupGate>
  );
}

function AdaptiveDashboardRoute() {
  const { status } = useAuth();

  if (status === "loading") {
    return <FullPageMessage message="Loading Enigma..." />;
  }

  if (status === "unauthenticated") {
    return <Navigate to="/login" replace />;
  }

  // Same as `HomeRoute`: this route does not go through `PrivateRoute`, so without an explicit
  // gate the whole workspace dashboard subtree is reachable while unconfigured.
  return (
    <SetupGate>
      <DashboardRoute />
    </SetupGate>
  );
}

const SETUP_PATH = "/setup";

/** Authentication only. Used by the setup route, which must not be setup-gated. */
function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth();

  if (status === "loading") {
    return <FullPageMessage message="Loading session..." />;
  }

  if (status === "unauthenticated") {
    return <Navigate to="/login" replace />;
  }

  return children;
}

/**
 * First-run gate: sends an authenticated user to `/setup` while no LLM endpoint is configured.
 * Skippable, and it only ever fires on an affirmative `"unconfigured"` -- `"unknown"` (still
 * loading, or the status call failed) falls through, so a status outage never blocks the app.
 * This is UX only; the backend is what actually refuses an unconfigured agent run.
 */
function SetupGate({ children }: { children: ReactNode }) {
  const { state } = useLlmStatus();
  const skipped = useLlmSetupSkipped();
  const location = useLocation();

  // Belt and braces: `/setup` never mounts this today, but a refactor that wrapped it in
  // `PrivateRoute` would otherwise produce an infinite redirect rather than a visible error.
  if (location.pathname === SETUP_PATH) {
    return children;
  }

  if (state === "unconfigured" && !skipped) {
    return (
      <Navigate
        to={SETUP_PATH}
        replace
        state={{ from: `${location.pathname}${location.search}` }}
      />
    );
  }

  return children;
}

function PrivateRoute({ children }: { children: ReactNode }) {
  return (
    <RequireAuth>
      <SetupGate>{children}</SetupGate>
    </RequireAuth>
  );
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
