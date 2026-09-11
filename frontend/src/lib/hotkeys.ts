/** A hotkey is a single letter or digit; the modifiers (Ctrl/Cmd + Alt) are fixed. */
const HOTKEY_PATTERN = /^[a-z0-9]$/i;

export function normalizeHotkey(value: string): string {
  const last = value.trim().slice(-1);
  return HOTKEY_PATTERN.test(last) ? last.toLowerCase() : "";
}

/**
 * Match a stored hotkey against a keydown. With Option held, macOS reports the *composed*
 * character in `event.key` (Option+D is `∂`, Option+T is `†`), so a letter hotkey never equals
 * `event.key` there; `event.code` names the physical key (`KeyD`, `Digit1`) regardless of layout
 * and modifiers, so it is the primary match and `event.key` the fallback.
 */
export function hotkeyMatches(hotkey: string | null | undefined, event: KeyboardEvent): boolean {
  const key = normalizeHotkey(hotkey ?? "");
  if (!key) {
    return false;
  }
  if (event.key.toLowerCase() === key) {
    return true;
  }
  const code = /[a-z]/.test(key) ? `Key${key.toUpperCase()}` : `Digit${key}`;
  return event.code === code;
}
