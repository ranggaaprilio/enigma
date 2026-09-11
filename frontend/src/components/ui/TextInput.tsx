export function TextInput({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
  autoFocus,
  required,
  disabled,
  hint,
  mono,
  listId,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: "text" | "password";
  placeholder?: string;
  autoFocus?: boolean;
  required?: boolean;
  disabled?: boolean;
  /** Short helper text rendered under the field (outside the label so labels stay exact). */
  hint?: string;
  mono?: boolean;
  /** Id of a `<datalist>` elsewhere on the page to drive suggestions. Degrades to a plain
   * input wherever `<datalist>` is unsupported, jsdom included. */
  listId?: string;
}) {
  return (
    <div>
      <label className="block text-sm font-medium">
        {label}
        <input
          className={`mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-ink disabled:cursor-not-allowed disabled:opacity-60 ${
            mono ? "font-mono text-sm" : ""
          }`}
          type={type}
          value={value}
          placeholder={placeholder}
          autoFocus={autoFocus}
          required={required}
          disabled={disabled}
          list={listId}
          onChange={(event) => onChange(event.target.value)}
        />
      </label>
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
    </div>
  );
}
