import { useLocation, useNavigate } from "react-router";

import { useAuth } from "../auth/context";
import { LlmSettingsPanel } from "../components/LlmSettings";
import { markLlmSetupSkipped } from "../platform/llmStatus";

/**
 * First-run setup. Deliberately chromeless and centered, mirroring `LoginRoute`: a sidebar full
 * of links that do not work yet is a poor first impression, and this reads as a continuation of
 * sign-up rather than a half-broken app.
 */
export function SetupRoute() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from;
  const isOwner = user?.role === "owner";

  function leave() {
    markLlmSetupSkipped();
    navigate(from ?? "/", { replace: true });
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas px-5 py-8 text-ink">
      <section className="w-full max-w-xl">
        <p className="text-xs font-semibold uppercase tracking-[0.16em] text-primary">ENIGMA</p>

        {isOwner ? (
          <>
            <h1 className="mt-2 text-3xl font-normal tracking-[-0.02em]">
              Connect a language model
            </h1>
            <p className="mt-3 text-sm text-body">
              Enigma drives coding agents through an OpenAI-compatible API. Point it at a
              provider &mdash; hosted, or a model running on your own machine &mdash; and agent
              runs, spec workflows and usage tracking all come online.
            </p>
            <p className="mt-2 text-sm text-body">
              Your API key is encrypted before it is stored and is never shown again. You can
              change any of this later in Settings.
            </p>

            <div className="mt-6">
              <LlmSettingsPanel
                variant="onboarding"
                onSaved={() => navigate(from ?? "/", { replace: true })}
              />
            </div>

            <div className="mt-4 border-t border-line pt-4">
              <button
                type="button"
                onClick={leave}
                className="text-sm text-body underline transition hover:text-ink"
              >
                Skip for now
              </button>
              <p className="mt-1 text-xs text-muted">
                You can explore Enigma, but agent runs stay blocked until a model is connected.
              </p>
            </div>
          </>
        ) : (
          <>
            <h1 className="mt-2 text-3xl font-normal tracking-[-0.02em]">
              Enigma isn&rsquo;t set up yet
            </h1>
            <p className="mt-3 text-sm text-body">
              A platform owner needs to connect an OpenAI-compatible language model before
              agents can run. Ask your platform owner to add one under Settings &mdash; you can
              keep browsing in the meantime.
            </p>
            <button
              type="button"
              onClick={leave}
              className="mt-6 rounded-md bg-primary px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary-active"
            >
              Continue to Enigma
            </button>
            {user?.email && (
              <p className="mt-3 text-xs text-muted">
                Signed in as {user.email} &middot; {user.role} access
              </p>
            )}
          </>
        )}
      </section>
    </main>
  );
}
