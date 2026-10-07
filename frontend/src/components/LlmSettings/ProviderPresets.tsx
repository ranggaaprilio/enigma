/**
 * Common OpenAI-compatible endpoints, offered as one-click chips.
 *
 * The chips are the real affordance: `TextInput`'s `listId` renders a `<datalist>`, which is
 * inert in jsdom and invisible until the user types, so it cannot be the only way to discover
 * these. Both are wired -- chips for discovery, datalist for autocomplete.
 */
export type ProviderPreset = {
  label: string;
  baseUrl: string;
  /** A model id that actually exists on that provider, used only when the field is empty. */
  model?: string;
  note?: string;
};

export const PROVIDER_PRESETS: ProviderPreset[] = [
  { label: "OpenAI", baseUrl: "https://api.openai.com/v1", model: "gpt-4.1-mini" },
  { label: "Ollama", baseUrl: "http://localhost:11434/v1", model: "qwen2.5-coder:7b" },
  { label: "LM Studio", baseUrl: "http://localhost:1234/v1" },
  { label: "OpenRouter", baseUrl: "https://openrouter.ai/api/v1" },
  {
    label: "vLLM",
    baseUrl: "http://localhost:8000/v1",
    note: "vLLM's default port is also Enigma's own backend port",
  },
  { label: "Together", baseUrl: "https://api.together.xyz/v1" },
  { label: "Groq", baseUrl: "https://api.groq.com/openai/v1", model: "llama-3.3-70b-versatile" },
];

export const BASE_URL_DATALIST_ID = "llm-base-url-suggestions";

export function ProviderPresetDatalist() {
  return (
    <datalist id={BASE_URL_DATALIST_ID}>
      {PROVIDER_PRESETS.map((preset) => (
        <option key={preset.label} value={preset.baseUrl} />
      ))}
    </datalist>
  );
}

export function ProviderPresetChips({
  onPick,
  disabled,
}: {
  onPick: (preset: ProviderPreset) => void;
  disabled?: boolean;
}) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      <span className="text-xs text-muted">Start from:</span>
      {PROVIDER_PRESETS.map((preset) => (
        <button
          key={preset.label}
          type="button"
          title={preset.note}
          disabled={disabled}
          onClick={() => onPick(preset)}
          className="rounded-full border border-line bg-white px-2 py-0.5 text-xs text-body transition hover:border-line-strong hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
        >
          {preset.label}
        </button>
      ))}
    </div>
  );
}
