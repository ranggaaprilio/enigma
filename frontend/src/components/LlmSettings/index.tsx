import { type FormEvent, useCallback, useEffect, useState } from "react";

import { useAuth } from "../../auth/context";
import {
  ApiError,
  deleteLlmSettings,
  getLlmSettings,
  testLlmSettings,
  updateLlmSettings,
  type LlmSettingsResponse,
  type LlmSettingsUpdatePayload,
} from "../../lib/api";
import { useLlmStatus } from "../../platform/llmStatus";
import { ConfirmModal } from "../ui/ConfirmModal";
import { Spinner } from "../ui/Spinner";
import { StatusPill } from "../ui/StatusPill";
import { TextInput } from "../ui/TextInput";
import { ConnectionResult, hostOf, type ProbeState } from "./ConnectionResult";
import {
  BASE_URL_DATALIST_ID,
  ProviderPresetChips,
  ProviderPresetDatalist,
  type ProviderPreset,
} from "./ProviderPresets";

const CUSTOM_MODEL = "__custom__";

export type LlmSettingsPanelProps = {
  variant: "settings" | "onboarding";
  /** Called after a successful save, once the status has been refreshed. */
  onSaved?: () => void;
};

/**
 * The platform-wide LLM configuration form, shared by `/settings` and the first-run `/setup`
 * screen. Only the platform owner may edit; everyone else gets a locked, read-only card fed
 * from the status endpoint (the owner-only GET would 403 for them, so it is never called).
 */
export function LlmSettingsPanel({ variant, onSaved }: LlmSettingsPanelProps) {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const status = useLlmStatus();

  const [settings, setSettings] = useState<LlmSettingsResponse | null>(null);
  const [loading, setLoading] = useState(isOwner);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [replacingKey, setReplacingKey] = useState(false);
  const [availableModels, setAvailableModels] = useState<string[]>([]);

  const [busy, setBusy] = useState<"save" | "test" | "clear" | null>(null);
  const [probe, setProbe] = useState<ProbeState>({ kind: "idle" });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  const applyServerState = useCallback((next: LlmSettingsResponse) => {
    setSettings(next);
    setBaseUrl(next.base_url ?? "");
    setModel(next.model ?? "");
    setApiKey("");
    setReplacingKey(false);
  }, []);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        applyServerState(await getLlmSettings(signal));
        setLoadError(null);
      } catch (caughtError) {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") return;
        setLoadError(
          caughtError instanceof ApiError
            ? caughtError.message
            : "Unable to load the LLM settings.",
        );
      } finally {
        setLoading(false);
      }
    },
    [applyServerState],
  );

  useEffect(() => {
    // A non-owner must never call the owner-only GET: it would 403 and render an error box
    // where a calm "ask your platform owner" state belongs.
    if (!isOwner) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [isOwner, load]);

  /** Any edit invalidates a previous test: a green result beside a changed URL is a lie. */
  function edit<T>(setter: (value: T) => void) {
    return (value: T) => {
      setter(value);
      setProbe({ kind: "idle" });
      setMessage(null);
    };
  }

  function pickPreset(preset: ProviderPreset) {
    setBaseUrl(preset.baseUrl);
    if (!model.trim() && preset.model) {
      setModel(preset.model);
    }
    setProbe({ kind: "idle" });
    setMessage(null);
  }

  function buildPayload(): LlmSettingsUpdatePayload {
    const payload: LlmSettingsUpdatePayload = {
      base_url: baseUrl.trim(),
      model: model.trim(),
    };
    // The key is omitted unless the user actually typed one. Sending "" would clear a stored
    // key that they never intended to touch -- this is the correctness crux of the form.
    if (replacingKey && apiKey.length > 0) {
      payload.api_key = apiKey;
    }
    return payload;
  }

  async function handleTest() {
    setError(null);
    setMessage(null);
    setBusy("test");
    setProbe({ kind: "testing", host: hostOf(baseUrl.trim()) });
    try {
      const result = await testLlmSettings({
        base_url: baseUrl.trim(),
        model: model.trim() || undefined,
        ...(replacingKey && apiKey.length > 0 ? { api_key: apiKey } : {}),
      });
      setProbe({ kind: "done", result });
      if (result.models && result.models.length > 0) {
        setAvailableModels(result.models);
      }
    } catch (caughtError) {
      setProbe({ kind: "idle" });
      setError(
        caughtError instanceof ApiError
          ? caughtError.message
          : "Unable to test the connection.",
      );
    } finally {
      setBusy(null);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setMessage(null);

    if (!baseUrl.trim() || !model.trim()) {
      setError("Enter a base URL and a model.");
      return;
    }

    setBusy("save");
    let saved: LlmSettingsResponse;
    try {
      saved = await updateLlmSettings(buildPayload());
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError
          ? caughtError.message
          : "Unable to save the LLM settings.",
      );
      return;
    } finally {
      setBusy(null);
    }

    applyServerState(saved);
    // Flip the gate synchronously *before* anything navigates: `refresh()` is a round trip,
    // and navigating while the status still says "unconfigured" bounces straight back to
    // /setup. Then confirm against the server.
    status.markConfigured(saved.source ?? "database");
    setMessage("LLM settings saved.");
    await status.refresh();
    onSaved?.();
  }

  async function handleClear() {
    setBusy("clear");
    setError(null);
    setMessage(null);
    try {
      await deleteLlmSettings();
      await load();
      await status.refresh();
      setMessage("Stored configuration cleared.");
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError
          ? caughtError.message
          : "Unable to clear the configuration.",
      );
    } finally {
      setBusy(null);
      setConfirmClear(false);
    }
  }

  // --- presentation -----------------------------------------------------------------------

  const effectiveBaseUrl = settings?.base_url ?? status.baseUrl;
  const effectiveModel = settings?.model ?? status.model;
  const effectiveSource = settings?.source ?? status.source;

  const pill =
    status.state === "configured" ? (
      <StatusPill
        status="healthy"
        label={effectiveSource === "environment" ? "From environment" : "Configured"}
      />
    ) : status.state === "unconfigured" ? (
      <StatusPill status="idle" label="Not configured" />
    ) : (
      <StatusPill status="starting" label="Checking" />
    );

  return (
    <section className="rounded-xl border border-line bg-white p-5 sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-medium uppercase tracking-[0.16em] text-ink/55">
            Agent runtime
          </p>
          <h2 className="mt-1 text-base font-semibold">Language model</h2>
          <p className="mt-1 text-sm text-body">
            Any OpenAI-compatible endpoint &mdash; hosted, or running on your own machine.
          </p>
        </div>
        {pill}
      </div>

      {effectiveBaseUrl && (
        <p className="mt-3 font-mono text-xs text-muted">
          {effectiveBaseUrl}
          {effectiveModel ? ` · ${effectiveModel}` : ""}
        </p>
      )}
      {effectiveSource === "environment" && (
        <p className="mt-2 rounded-md border border-line bg-panel p-3 text-xs text-ink/65">
          Currently supplied by environment variables on the server. Saving here stores the
          configuration in Enigma, which takes precedence from the next agent run onwards.
        </p>
      )}

      {!isOwner ? (
        <div className="mt-5 rounded-md border border-line bg-panel p-3 text-sm text-body">
          Only the platform owner can change these settings.
        </div>
      ) : loading ? (
        <p className="mt-5 text-sm text-ink/60">Loading settings...</p>
      ) : loadError ? (
        <div className="mt-5">
          <div className="rounded-md border border-danger/30 bg-[#fff5f2] px-3 py-2 text-sm text-danger">
            {loadError}
          </div>
          <button
            type="button"
            onClick={() => void load()}
            className="mt-3 rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel"
          >
            Retry
          </button>
        </div>
      ) : (
        <form className="mt-5" onSubmit={handleSubmit}>
          <ProviderPresetDatalist />
          <div className="space-y-5">
            <div>
              <TextInput
                label="Base URL"
                value={baseUrl}
                onChange={edit(setBaseUrl)}
                mono
                placeholder="https://api.openai.com/v1"
                listId={BASE_URL_DATALIST_ID}
                hint="The OpenAI-compatible root. Include /v1 if your provider uses it."
              />
              <ProviderPresetChips onPick={pickPreset} disabled={busy !== null} />
            </div>

            <ApiKeyField
              hasStoredKey={settings?.has_api_key ?? false}
              maskedKey={settings?.masked_api_key ?? null}
              replacing={replacingKey}
              value={apiKey}
              onStartReplace={() => setReplacingKey(true)}
              onCancelReplace={() => {
                setReplacingKey(false);
                setApiKey("");
              }}
              onChange={edit(setApiKey)}
            />

            <ModelField
              value={model}
              models={availableModels}
              onChange={edit(setModel)}
            />
          </div>

          <ConnectionResult state={probe} baseUrl={baseUrl.trim()} />

          {message && (
            <div className="mt-4 rounded-md border border-signal/30 bg-[#f0faf5] px-3 py-2 text-sm text-signal">
              {message}
            </div>
          )}
          {error && (
            <div className="mt-4 rounded-md border border-danger/30 bg-[#fff5f2] px-3 py-2 text-sm text-danger">
              {error}
            </div>
          )}

          <div className="mt-6 flex flex-col gap-2 border-t border-line pt-4 sm:flex-row sm:items-center sm:justify-between">
            <button
              type="button"
              onClick={() => void handleTest()}
              disabled={!baseUrl.trim() || busy !== null}
              aria-busy={busy === "test"}
              className="inline-flex w-full items-center justify-center gap-2 rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto"
            >
              {busy === "test" && <Spinner />}
              {busy === "test" ? "Testing..." : "Test connection"}
            </button>
            <button
              type="submit"
              disabled={busy !== null}
              aria-busy={busy === "save"}
              className="w-full rounded-md bg-primary px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45 sm:w-auto"
            >
              {busy === "save"
                ? "Saving..."
                : variant === "onboarding"
                  ? "Save and continue"
                  : "Save settings"}
            </button>
          </div>

          {variant === "settings" && settings?.source === "database" && (
            <div className="mt-6 rounded-md border border-danger/40 bg-white p-4">
              <p className="text-sm font-semibold text-ink">Clear configuration</p>
              <p className="mt-1 text-xs text-body">
                Removes the stored endpoint and API key. Enigma falls back to the server's
                environment variables, if any are set.
              </p>
              <button
                type="button"
                onClick={() => setConfirmClear(true)}
                disabled={busy !== null}
                className="mt-3 rounded-md border border-danger/40 bg-white px-3 py-2 text-sm font-medium text-danger transition hover:bg-danger/10 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Clear configuration
              </button>
            </div>
          )}
        </form>
      )}

      {confirmClear && (
        <ConfirmModal
          title="Clear LLM configuration?"
          body="Agent runs will stop working until a new endpoint is configured, unless the server has environment variables set."
          confirmLabel="Clear configuration"
          busy={busy === "clear"}
          onCancel={() => setConfirmClear(false)}
          onConfirm={() => void handleClear()}
        />
      )}
    </section>
  );
}

/**
 * Write-only API key, following `EnvVarEditor`'s doctrine: a stored secret is shown masked and
 * the input is never prefilled, so a save cannot round-trip a value the user never saw.
 */
function ApiKeyField({
  hasStoredKey,
  maskedKey,
  replacing,
  value,
  onStartReplace,
  onCancelReplace,
  onChange,
}: {
  hasStoredKey: boolean;
  maskedKey: string | null;
  replacing: boolean;
  value: string;
  onStartReplace: () => void;
  onCancelReplace: () => void;
  onChange: (value: string) => void;
}) {
  if (hasStoredKey && !replacing) {
    return (
      <div>
        <p className="text-sm font-medium">API key</p>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-md border border-line bg-panel p-3">
          <p className="font-mono text-sm text-ink/55">{maskedKey ?? "********"}</p>
          <button
            type="button"
            onClick={onStartReplace}
            className="rounded-md border border-line bg-white px-3 py-1.5 text-xs font-medium transition hover:bg-panel"
          >
            Replace key
          </button>
        </div>
        <p className="mt-1 text-xs text-muted">
          The key cannot be read back once saved &mdash; the masked value is deliberate, not a
          bug. Use &ldquo;Replace key&rdquo; to overwrite it.
        </p>
      </div>
    );
  }

  return (
    <div>
      <TextInput
        label={hasStoredKey ? "New API key" : "API key"}
        value={value}
        onChange={onChange}
        type="password"
        autoFocus={replacing}
        placeholder={hasStoredKey ? "" : "Leave empty for a keyless local endpoint"}
        hint="Encrypted before it is stored, and never shown again after saving."
      />
      {hasStoredKey && (
        <button
          type="button"
          onClick={onCancelReplace}
          className="mt-2 text-xs text-body underline transition hover:text-ink"
        >
          Cancel
        </button>
      )}
    </div>
  );
}

/**
 * A dropdown once a connection test has returned a model list, free text otherwise. Free text
 * is the default because plenty of gateways serve `/chat/completions` without `/models`, so the
 * list is an enhancement and never a requirement.
 */
function ModelField({
  value,
  models,
  onChange,
}: {
  value: string;
  models: string[];
  onChange: (value: string) => void;
}) {
  const [manual, setManual] = useState(false);
  const useSelect = models.length > 0 && !manual;
  const missing = models.length > 0 && value.trim().length > 0 && !models.includes(value);

  if (!useSelect) {
    return (
      <div>
        <TextInput
          label="Model"
          value={value}
          onChange={onChange}
          mono
          placeholder="gpt-4.1-mini"
          hint="Test the connection to load the endpoint's model list, or type an id."
        />
        {models.length > 0 && (
          <button
            type="button"
            onClick={() => setManual(false)}
            className="mt-2 text-xs text-body underline transition hover:text-ink"
          >
            Choose from the endpoint's list
          </button>
        )}
      </div>
    );
  }

  return (
    <div>
      <label className="block text-sm font-medium">
        Model
        <select
          className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 text-sm outline-none transition focus:border-ink"
          value={models.includes(value) ? value : CUSTOM_MODEL}
          onChange={(event) => {
            if (event.target.value === CUSTOM_MODEL) {
              setManual(true);
              return;
            }
            onChange(event.target.value);
          }}
        >
          {!models.includes(value) && value.trim().length > 0 && (
            <option value={CUSTOM_MODEL}>{value} (not in list)</option>
          )}
          {models.map((id) => (
            <option key={id} value={id}>
              {id}
            </option>
          ))}
          <option value={CUSTOM_MODEL}>Enter manually...</option>
        </select>
      </label>
      {missing && (
        <p className="mt-2 rounded-md border border-warn/30 bg-[#fffbf0] px-3 py-2 text-xs text-warn">
          {value} was not in the endpoint's list. It may still work.
        </p>
      )}
    </div>
  );
}
