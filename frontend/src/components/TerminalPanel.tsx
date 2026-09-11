import { useEffect, useRef, useState } from "react";
import type { FitAddon } from "@xterm/addon-fit";
import type { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";

import {
  ApiError,
  createTerminalSession,
  deleteTerminalSession,
  getTerminalSessionSocketUrl,
  type TerminalSessionResponse,
  type WorkspaceResponse,
} from "../lib/api";
import { useActiveWorktree } from "../lib/activeWorktree";
import { useReconnectingWebSocket } from "../lib/useReconnectingWebSocket";

const STATUS_LABELS: Record<string, string> = {
  idle: "Idle",
  connecting: "Connecting",
  live: "Connected",
  reconnecting: "Reconnecting",
  disconnected: "Disconnected",
};

export function TerminalPanel({ workspace }: { workspace: WorkspaceResponse }) {
  const [activeWorktree] = useActiveWorktree(workspace.id);
  const worktreeId = activeWorktree?.id ?? null;
  const containerRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const resizeObserverRef = useRef<ResizeObserver | null>(null);
  // The hook owns the socket; keep the latest `send` in a ref so xterm's onData/resize
  // handlers (registered once) always target the current connection.
  const sendRef = useRef<(data: string | ArrayBufferLike | Blob | ArrayBufferView) => void>(
    () => {},
  );
  const [session, setSession] = useState<TerminalSessionResponse | null>(null);
  const [terminalReady, setTerminalReady] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function handleTextMessage(value: string) {
    try {
      const payload = JSON.parse(value) as { type?: string; message?: string };
      if (payload.type === "error" && payload.message) {
        setError(payload.message);
      }
    } catch {
      terminalRef.current?.write(value);
    }
  }

  function fitAndSendSize() {
    const fitAddon = fitAddonRef.current;
    const terminal = terminalRef.current;
    if (!fitAddon || !terminal) {
      return;
    }
    try {
      fitAddon.fit();
      sendRef.current(
        JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows }),
      );
    } catch {
      return;
    }
  }

  // Create the backend session and the xterm instance once per workspace/worktree. The
  // socket itself is managed by useReconnectingWebSocket below.
  useEffect(() => {
    let cancelled = false;

    async function init() {
      setError(null);
      setStopped(false);
      try {
        const terminalSession = await createTerminalSession(workspace.id, { name: "Terminal" });
        if (cancelled) {
          return;
        }
        setSession(terminalSession);

        const [{ Terminal }, { FitAddon }] = await Promise.all([
          import("@xterm/xterm"),
          import("@xterm/addon-fit"),
        ]);
        if (cancelled || !containerRef.current) {
          return;
        }

        const terminal = new Terminal({
          convertEol: true,
          cursorBlink: true,
          fontFamily:
            'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
          fontSize: 13,
          scrollback: 10_000,
          scrollOnUserInput: true,
          theme: {
            background: "#fafaf7",
            foreground: "#26251e",
            cursor: "#f54e00",
            selectionBackground: "#dfa88f",
          },
        });
        const fitAddon = new FitAddon();
        terminal.loadAddon(fitAddon);
        terminal.open(containerRef.current);
        terminal.focus();
        terminalRef.current = terminal;
        fitAddonRef.current = fitAddon;

        terminal.onData((data) => sendRef.current(new TextEncoder().encode(data)));

        if (containerRef.current && "ResizeObserver" in window) {
          const observer = new ResizeObserver(() => fitAndSendSize());
          observer.observe(containerRef.current);
          resizeObserverRef.current = observer;
        }

        setTerminalReady(true);
      } catch (caughtError) {
        if (cancelled) {
          return;
        }
        setError(
          caughtError instanceof ApiError ? caughtError.message : "Unable to open terminal.",
        );
      }
    }

    void init();

    return () => {
      cancelled = true;
      resizeObserverRef.current?.disconnect();
      resizeObserverRef.current = null;
      terminalRef.current?.dispose();
      terminalRef.current = null;
      fitAddonRef.current = null;
      setTerminalReady(false);
      setSession(null);
    };
  }, [workspace.id, worktreeId]);

  const socketUrl =
    session && terminalReady && !stopped
      ? getTerminalSessionSocketUrl(workspace.id, session.id, worktreeId)
      : null;

  const { connectionState, send } = useReconnectingWebSocket(socketUrl, {
    binaryType: "arraybuffer",
    onOpen: () => {
      setError(null);
      fitAndSendSize();
    },
    onMessage: (message) => {
      if (typeof message.data === "string") {
        handleTextMessage(message.data);
        return;
      }
      terminalRef.current?.write(new Uint8Array(message.data as ArrayBuffer));
    },
    onError: () => setError("Terminal connection failed."),
  });

  useEffect(() => {
    sendRef.current = send;
  }, [send]);

  async function copySelection() {
    const selection = terminalRef.current?.getSelection();
    if (!selection) {
      return;
    }
    await navigator.clipboard?.writeText(selection);
  }

  async function pasteClipboard() {
    const text = await navigator.clipboard?.readText();
    if (text) {
      send(new TextEncoder().encode(text));
    }
  }

  async function stopSession() {
    if (!session) {
      return;
    }
    setError(null);
    try {
      await deleteTerminalSession(workspace.id, session.id);
      setStopped(true); // nulls the socket URL → hook closes the connection, no reconnect
      setSession((current) => (current ? { ...current, status: "stopped" } : current));
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to stop terminal.");
    }
  }

  const statusLabel = error ? "Error" : (STATUS_LABELS[connectionState] ?? "Idle");

  return (
    <section className="rounded-md border border-line bg-white p-5 shadow-soft">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">
            Terminal
          </p>
          <h3 className="mt-1 text-base font-semibold">{workspace.name}</h3>
          <p className="mt-0.5 text-xs text-ink/55">
            Branch:{" "}
            <span className="font-medium text-ink/75">
              {activeWorktree ? activeWorktree.branch : "base"}
            </span>
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full bg-white px-3 py-1 text-sm font-semibold text-ink/65">
            {statusLabel}
          </span>
          <button
            className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel"
            type="button"
            onClick={() => void copySelection()}
          >
            Copy
          </button>
          <button
            className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel"
            type="button"
            onClick={() => void pasteClipboard()}
          >
            Paste
          </button>
          <button
            className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={!session || session.status !== "active"}
            onClick={() => void stopSession()}
          >
            Stop
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger">
          {error}
        </div>
      )}

      <div
        ref={containerRef}
        className="terminal-frame h-[560px] min-h-[360px] overflow-auto rounded-lg border border-line bg-panel p-2"
      />
    </section>
  );
}
