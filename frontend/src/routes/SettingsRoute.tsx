import { AppShell } from "../components/AppShell";
import { LlmSettingsPanel } from "../components/LlmSettings";

export function SettingsRoute() {
  return (
    <AppShell>
      <main className="mx-auto w-full max-w-3xl px-6 py-10">
        <div className="mb-8">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-ink/55">
            Platform
          </p>
          <h1 className="mt-1 text-2xl font-semibold text-ink">Settings</h1>
          <p className="mt-2 text-sm text-muted">
            Configuration that applies to every project and workspace in this Enigma instance.
          </p>
        </div>

        <LlmSettingsPanel variant="settings" />

        <section className="mt-6 rounded-xl border border-line bg-panel p-5 text-sm text-body">
          <p className="font-semibold text-ink">Where this is used</p>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            <li>Agent runs and tool calls</li>
            <li>Spec Kit workflows, which drive one agent run per phase</li>
            <li>Usage and cost tracking, which records the provider and model per run</li>
          </ul>
          <p className="mt-3 text-xs text-muted">
            Agent runs are blocked until a language model is connected.
          </p>
        </section>
      </main>
    </AppShell>
  );
}
