import { usePresence } from "../lib/usePresence";

const MAX_VISIBLE_AVATARS = 4;

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0]!.slice(0, 2).toUpperCase();
  return `${parts[0]![0]}${parts[parts.length - 1]![0]}`.toUpperCase();
}

export function PresenceAvatars({ workspaceId }: { workspaceId: string | null }) {
  const { viewers } = usePresence(workspaceId);

  if (viewers.length === 0) return null;

  const visible = viewers.slice(0, MAX_VISIBLE_AVATARS);
  const overflow = viewers.length - visible.length;

  return (
    <div className="flex items-center" data-testid="presence-avatars">
      {visible.map((viewer, index) => (
        <span
          key={viewer.user_id}
          title={viewer.name}
          className="flex h-7 w-7 items-center justify-center rounded-full border border-line bg-white text-xs font-semibold text-ink ring-2 ring-canvas"
          style={index > 0 ? { marginLeft: "-0.5rem" } : undefined}
        >
          {initials(viewer.name)}
        </span>
      ))}
      {overflow > 0 && (
        <span
          title={`${overflow} more`}
          className="flex h-7 w-7 items-center justify-center rounded-full border border-line bg-panel text-xs font-semibold text-muted ring-2 ring-canvas"
          style={{ marginLeft: "-0.5rem" }}
        >
          +{overflow}
        </span>
      )}
    </div>
  );
}
