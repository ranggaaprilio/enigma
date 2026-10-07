export function TextArea({
  label,
  value,
  onChange,
  placeholder,
  disabled,
  hint,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  /** Short helper text rendered under the field (outside the label so labels stay exact). */
  hint?: string;
}) {
  return (
    <div>
      <label className="block text-sm font-medium">
        {label}
        <textarea
          className="mt-2 min-h-[220px] w-full resize-y rounded-md border border-line bg-white px-3 py-2 font-mono text-sm outline-none transition focus:border-ink disabled:cursor-not-allowed disabled:opacity-60"
          value={value}
          placeholder={placeholder}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        />
      </label>
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
    </div>
  );
}
