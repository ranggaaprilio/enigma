import { describe, expect, it } from "vitest";

import { hotkeyMatches, normalizeHotkey } from "./hotkeys";

function keydown(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", init);
}

describe("normalizeHotkey", () => {
  it("keeps a single letter or digit, lower-cased", () => {
    expect(normalizeHotkey("D")).toBe("d");
    expect(normalizeHotkey("7")).toBe("7");
  });

  it("keeps only the last typed character so the field never grows past one key", () => {
    expect(normalizeHotkey("dt")).toBe("t");
  });

  it("rejects chords and symbols, which the listener could never match", () => {
    expect(normalizeHotkey("Ctrl+T")).toBe("t");
    expect(normalizeHotkey("+")).toBe("");
    expect(normalizeHotkey(" ")).toBe("");
  });
});

describe("hotkeyMatches", () => {
  it("matches the plain key on layouts that report it", () => {
    expect(hotkeyMatches("d", keydown({ key: "d", code: "KeyD" }))).toBe(true);
    expect(hotkeyMatches("D", keydown({ key: "d", code: "KeyD" }))).toBe(true);
  });

  it("matches on the physical key when Option composes the character", () => {
    expect(hotkeyMatches("d", keydown({ key: "∂", code: "KeyD" }))).toBe(true);
    expect(hotkeyMatches("t", keydown({ key: "†", code: "KeyT" }))).toBe(true);
    expect(hotkeyMatches("e", keydown({ key: "Dead", code: "KeyE" }))).toBe(true);
    expect(hotkeyMatches("1", keydown({ key: "¡", code: "Digit1" }))).toBe(true);
  });

  it("does not match a different key or an unset hotkey", () => {
    expect(hotkeyMatches("d", keydown({ key: "†", code: "KeyT" }))).toBe(false);
    expect(hotkeyMatches(null, keydown({ key: "d", code: "KeyD" }))).toBe(false);
    expect(hotkeyMatches("", keydown({ key: "d", code: "KeyD" }))).toBe(false);
  });
});
