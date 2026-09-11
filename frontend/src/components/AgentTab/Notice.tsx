import type { ReactNode } from "react";

export function Notice({
  children,
  tone,
  testId,
}: {
  children: ReactNode;
  tone: "warn" | "danger";
  testId?: string;
}) {
  const toneClass =
    tone === "warn"
      ? "border-warn/30 bg-[#fffbf0] text-warn"
      : "border-danger/30 bg-[#fff5f2] text-danger";
  return (
    <div
      className={`flex items-center gap-2 rounded-md border px-3 py-2 text-sm ${toneClass}`}
      data-testid={testId}
    >
      {children}
    </div>
  );
}
