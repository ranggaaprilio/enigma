import type { CustomCommandResponse } from "../../lib/api";

/**
 * `${ENIGMA_PREVIEW_URL:<selector>}` templates, one per service command -- the most common
 * cross-service wiring a user reaches for. This does not attempt to reproduce the backend's
 * full selector-derivation chain (`env_templates.py`'s slug/tail/role/id fallback); it only
 * needs to be a plausible, clickable starting point -- the backend's write-time check is what
 * actually validates a selector and reports an `unknown_service`/`ambiguous_service` warning
 * if this guess doesn't match.
 */
export const TEMPLATE_SUGGESTIONS_DATALIST_ID = "enigma-preview-template-suggestions";

function selectorFor(command: CustomCommandResponse): string {
  const slug = command.name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || command.id;
}

export function templateSuggestionsFor(serviceCommands: CustomCommandResponse[]): string[] {
  return serviceCommands.map((command) => `\${ENIGMA_PREVIEW_URL:${selectorFor(command)}}`);
}

/** Rendered once per page -- any `TextInput` can opt in via its `listId` prop. */
export function TemplateSuggestionsDatalist({
  serviceCommands,
}: {
  serviceCommands: CustomCommandResponse[];
}) {
  const suggestions = templateSuggestionsFor(serviceCommands);
  return (
    <datalist id={TEMPLATE_SUGGESTIONS_DATALIST_ID}>
      {suggestions.map((suggestion) => (
        <option key={suggestion} value={suggestion} />
      ))}
    </datalist>
  );
}

/**
 * Click-to-insert chips -- the only option for a `<textarea>`, which cannot carry a `list`
 * attribute at all. Insertion appends to the current value; there is no cursor-precise API on
 * `TextArea` and adding one is not worth it for this.
 */
export function TemplateSuggestionChips({
  serviceCommands,
  onInsert,
}: {
  serviceCommands: CustomCommandResponse[];
  onInsert: (text: string) => void;
}) {
  const suggestions = templateSuggestionsFor(serviceCommands);
  if (suggestions.length === 0) {
    return null;
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {suggestions.map((suggestion) => (
        <button
          key={suggestion}
          type="button"
          onClick={() => onInsert(suggestion)}
          className="rounded-full border border-line bg-white px-2 py-0.5 font-mono text-xs text-ink/70 transition hover:bg-panel"
          title={`Insert ${suggestion}`}
        >
          {suggestion}
        </button>
      ))}
    </div>
  );
}
