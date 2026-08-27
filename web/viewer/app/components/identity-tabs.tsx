import { Link, useParams } from "react-router";

import { ActivityBadge } from "~/components/activity-badge";
import { cn } from "~/lib/utils";

const TABS = [
  { key: "timeline", label: "Timeline", path: "" },
  { key: "recap", label: "Recap", path: "/recap" },
  { key: "mindlog", label: "Mind log", path: "/mindlog" },
  { key: "mindlog2", label: "Mind log v2", path: "/mindlog2" },
  { key: "thinkers", label: "Thinkers", path: "/thinkers" },
  { key: "health", label: "Health", path: "/health" },
  { key: "usage", label: "Usage", path: "/usage" },
  { key: "chat", label: "Chat", path: "/chat" },
  { key: "memories", label: "Memories", path: "/memories" },
  { key: "proposals", label: "Proposals", path: "/proposals" },
  { key: "shadow", label: "Shadow gate", path: "/shadow" },
  { key: "config", label: "Config", path: "/config" },
] as const;

/** Header row shared by the identity sub-pages: breadcrumb + tab links. */
export function IdentityTabs({
  identityId,
  live,
  active,
  name,
  actions,
}: {
  identityId: string;
  live: boolean;
  active: (typeof TABS)[number]["key"];
  name?: string;
  /** Page-specific controls (e.g. mind-log search) — rendered at the far
   * right of this sticky header so they stay reachable at any scroll. */
  actions?: React.ReactNode;
}) {
  useParams(); // keep router context
  const base = `/i/${encodeURIComponent(identityId)}`;
  const displayName = name ?? identityId.split("~").pop() ?? identityId;

  return (
    <div className="sticky top-12 z-40 -mx-4 mb-4 flex flex-wrap items-center gap-3 border-b bg-background/95 px-4 py-2 backdrop-blur">
      <Link to="/" className="text-sm text-muted-foreground hover:underline">
        identities
      </Link>
      <span className="text-muted-foreground">/</span>
      <h1 className="font-mono text-lg font-semibold">{displayName}</h1>
      <ActivityBadge identityId={identityId} live={live} />
      <nav className="ml-auto flex max-w-full items-center gap-1 overflow-x-auto rounded-lg border p-0.5 max-sm:ml-0 max-sm:w-full">
        {TABS.map((tab) => (
          <Link
            key={tab.key}
            to={`${base}${tab.path}`}
            className={cn(
              "shrink-0 rounded-md px-2.5 py-1 text-xs",
              tab.key === active
                ? "bg-accent font-medium"
                : "text-muted-foreground hover:text-foreground"
            )}
          >
            {tab.label}
          </Link>
        ))}
      </nav>
      {actions}
    </div>
  );
}
