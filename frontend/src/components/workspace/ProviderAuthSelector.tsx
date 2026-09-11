import type { GitProvider, GitProviderChoice, RepositoryAuthType } from "../../lib/api";
import {
  PROVIDER_HELP,
  detectProvider,
  providerLabel,
  resolveProvider,
} from "../../lib/gitProviders";
import { TextInput } from "../ui/TextInput";

const PROVIDER_ORDER: GitProvider[] = ["github", "gitlab", "bitbucket", "generic"];

function PillGroup<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  disabled?: boolean;
}) {
  return (
    <div>
      <p className="text-sm font-medium">{label}</p>
      <ol role="radiogroup" aria-label={label} className="mt-2 flex flex-wrap gap-2">
        {options.map((option) => {
          const active = option.value === value;
          return (
            <li key={option.value}>
              <button
                type="button"
                role="radio"
                aria-checked={active}
                disabled={disabled}
                onClick={() => onChange(option.value)}
                className={`rounded-full border px-3 py-1.5 text-sm transition disabled:opacity-60 ${
                  active
                    ? "border-ink bg-ink text-white"
                    : "border-line bg-white text-body hover:bg-panel"
                }`}
              >
                {option.label}
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export function ProviderAuthSelector({
  repoUrl,
  provider,
  onProviderChange,
  authType,
  onAuthTypeChange,
  token,
  onTokenChange,
  username,
  onUsernameChange,
  disabled,
}: {
  repoUrl: string;
  provider: GitProviderChoice;
  onProviderChange: (provider: GitProviderChoice) => void;
  authType: RepositoryAuthType;
  onAuthTypeChange: (authType: RepositoryAuthType) => void;
  token: string;
  onTokenChange: (token: string) => void;
  username: string;
  onUsernameChange: (username: string) => void;
  disabled?: boolean;
}) {
  const detected = detectProvider(repoUrl);
  const resolved = resolveProvider(repoUrl, provider);
  const help = PROVIDER_HELP[resolved];

  return (
    <div className="space-y-4">
      <PillGroup<GitProviderChoice>
        label="Git provider"
        value={provider}
        disabled={disabled}
        onChange={onProviderChange}
        options={[
          { value: "auto", label: `Auto (${providerLabel(detected)})` },
          ...PROVIDER_ORDER.map((slug) => ({
            value: slug,
            label: slug === "generic" ? "Other git host" : providerLabel(slug),
          })),
        ]}
      />

      <PillGroup<RepositoryAuthType>
        label="Authentication"
        value={authType}
        disabled={disabled}
        onChange={onAuthTypeChange}
        options={[
          { value: "personal_access_token", label: "Access token" },
          { value: "ssh_key", label: "SSH deploy key" },
        ]}
      />

      {authType === "personal_access_token" ? (
        <div className="space-y-3">
          {(help.requiresUsername || resolved === "generic") && (
            <TextInput
              label={help.requiresUsername ? "Username" : "Username (optional)"}
              value={username}
              onChange={onUsernameChange}
              disabled={disabled}
            />
          )}
          <TextInput
            label={help.tokenLabel}
            type="password"
            value={token}
            onChange={onTokenChange}
            disabled={disabled}
            hint={help.tokenHelp}
          />
          <p className="text-xs text-muted">
            Tokens need an HTTPS repository URL. They are encrypted at rest and never shown again.
          </p>
        </div>
      ) : (
        <div className="rounded-md border border-line bg-canvas p-3 text-sm text-body">
          <p className="font-medium text-ink">No secret to paste</p>
          <p className="mt-1">
            A key pair is generated server-side after the workspace is created; the private key
            never leaves the platform. {help.sshHelp}
          </p>
          <p className="mt-1 text-xs text-muted">
            SSH keys need an SSH repository URL such as git@{resolved === "generic" ? "host" : `${providerLabel(resolved).toLowerCase()}.com`}:owner/repo.git.
          </p>
        </div>
      )}
    </div>
  );
}
