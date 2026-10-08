import type { ReactNode } from "react";

export function Notice({
  children,
  tone,
  testId,
}: {
  children: ReactNode;
  tone: "warn" | "danger" | "neutral";
  testId?: string;
}) {
  const toneClass = {
    warn: "border-warn/30 bg-[#fffbf0] text-warn",
    danger: "border-danger/30 bg-[#fff5f2] text-danger",
    neutral: "border-line bg-panel text-muted",
  }[tone];
  return (
    <div
      className={`flex items-center gap-2 rounded-md border px-3 py-2 text-sm ${toneClass}`}
      data-testid={testId}
      // Warnings appear mid-session (reconnecting, interrupted): announce them politely.
      role={tone === "warn" ? "status" : undefined}
    >
      {children}
    </div>
  );
}
