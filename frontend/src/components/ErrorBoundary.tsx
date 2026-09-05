import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * Catches render/lifecycle errors anywhere in the subtree so a single component throw does
 * not blank the whole SPA. Shows a recoverable fallback with a reload action.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface for local debugging / future error-reporting hookup.
    console.error("Unhandled UI error:", error, info.componentStack);
  }

  handleReload = () => {
    window.location.reload();
  };

  render(): ReactNode {
    if (this.state.error) {
      if (this.props.fallback) {
        return this.props.fallback;
      }
      return (
        <div
          role="alert"
          className="flex min-h-screen flex-col items-center justify-center gap-4 bg-canvas px-6 text-center text-ink"
        >
          <h1 className="text-2xl font-semibold">Something went wrong</h1>
          <p className="max-w-md text-sm text-body">
            The interface hit an unexpected error. Reloading usually fixes it. If the problem
            persists, contact your administrator.
          </p>
          <button
            className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary-active"
            type="button"
            onClick={this.handleReload}
          >
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
