export type UsageChartPoint = {
  label: string;
  value: number;
  tooltip: string;
};

type UsageChartProps = {
  points: UsageChartPoint[];
  ariaLabel: string;
};

// A lightweight single-series bar chart: magnitude over time in the brand hue.
// No dependency — inline flexbox bars anchored to a baseline, per-bar hover via title.
export function UsageChart({ points, ariaLabel }: UsageChartProps) {
  if (points.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center rounded-md border border-dashed border-line text-sm text-ink/55">
        No usage in this period.
      </div>
    );
  }

  const max = Math.max(...points.map((p) => p.value), 0);
  // When every bar is a fraction of the widest one, show the tallest at full height.
  const heightPct = (value: number) => (max <= 0 ? 2 : Math.max(2, (value / max) * 100));

  // Label only a few ticks so the axis stays readable at any range.
  const tickEvery = Math.ceil(points.length / 8);

  return (
    <div role="img" aria-label={ariaLabel}>
      <div className="flex h-40 items-end gap-[2px] overflow-x-auto">
        {points.map((point, index) => (
          <div
            key={`${point.label}-${index}`}
            className="group relative flex min-w-[6px] flex-1 items-end"
            style={{ height: "100%" }}
          >
            <div
              className="w-full rounded-t bg-primary/80 transition-colors group-hover:bg-primary"
              style={{ height: `${heightPct(point.value)}%` }}
              title={point.tooltip}
            />
          </div>
        ))}
      </div>
      <div className="mt-1 flex gap-[2px] text-[10px] text-ink/45">
        {points.map((point, index) => (
          <div key={`${point.label}-tick-${index}`} className="min-w-[6px] flex-1 text-center">
            {index % tickEvery === 0 ? point.label : ""}
          </div>
        ))}
      </div>
    </div>
  );
}
