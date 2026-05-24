import { useEffect, useRef, useState } from "react";
import { CircleUser, ExternalLink, LogOut, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  onSynced: () => Promise<void> | void;
}

const SOURCES = ["gmail", "slack"] as const;

// Each entry becomes a "Connect <label>" link inside the account dropdown.
// The href points at the backend's generic OAuth authorize route, which
// redirects to the provider's consent screen and back through /oauth/<p>/callback.
const OAUTH_PROVIDERS: { label: string; href: string }[] = [
  { label: "Gmail", href: "/oauth/google/authorize" },
  { label: "Slack", href: "/oauth/slack/authorize" },
];

export function Topbar({ onSynced }: Props) {
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [email, setEmail] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);

  useEffect(() => {
    api
      .health()
      .then(() => setHealthy(true))
      .catch(() => setHealthy(false));
    api
      .whoami()
      .then((r) => setEmail(r?.email ?? null))
      .catch(() => setEmail(null));
  }, []);

  const sync = async () => {
    setSyncing(true);
    const toastId = toast.loading("Syncing…");
    try {
      const results = await Promise.allSettled(SOURCES.map((s) => api.poll(s)));
      let created = 0;
      let fetched = 0;
      const failed: string[] = [];
      results.forEach((r, i) => {
        if (r.status === "fulfilled") {
          created += r.value.tasks_created;
          fetched += r.value.fetched;
        } else {
          failed.push(SOURCES[i]);
        }
      });
      if (failed.length === SOURCES.length) {
        toast.error(`Sync failed: ${failed.join(", ")}`, { id: toastId });
      } else {
        const parts = [
          `${fetched} fetched`,
          `${created} new task${created === 1 ? "" : "s"}`,
        ];
        if (failed.length) parts.push(`${failed.join(", ")} failed`);
        toast.success(`Synced: ${parts.join(" · ")}`, { id: toastId });
        await onSynced();
      }
    } finally {
      setSyncing(false);
    }
  };

  const logout = async () => {
    await api.logout();
    location.href = "/";
  };

  return (
    <header className="border-b bg-card">
      <div className="mx-auto flex max-w-2xl items-center gap-2 px-4 py-3">
        <span
          className={cn(
            "inline-block h-2 w-2 rounded-full",
            healthy == null
              ? "bg-muted-foreground"
              : healthy
                ? "bg-emerald-500"
                : "bg-destructive",
          )}
          title={
            healthy == null ? "checking" : healthy ? "healthy" : "unreachable"
          }
        />
        <h1 className="flex-1 text-base font-semibold">Task Agent</h1>
        <Button size="sm" variant="outline" onClick={sync} disabled={syncing}>
          <RefreshCw className={cn("h-4 w-4", syncing && "animate-spin")} />
          Sync
        </Button>
        {email && <AccountMenu email={email} onLogout={logout} />}
      </div>
    </header>
  );
}

function AccountMenu({
  email,
  onLogout,
}: {
  email: string;
  onLogout: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <Button
        size="icon"
        variant="ghost"
        onClick={() => setOpen((v) => !v)}
        aria-label="Account menu"
        aria-haspopup="menu"
        aria-expanded={open}
        title={email}
      >
        <CircleUser className="h-5 w-5" />
      </Button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-50 mt-2 w-56 overflow-hidden rounded-md border bg-card text-card-foreground shadow-md"
        >
          <div className="truncate border-b px-3 py-2 text-xs text-muted-foreground">
            {email}
          </div>
          <div className="py-1">
            <div className="px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Connect
            </div>
            {OAUTH_PROVIDERS.map((p) => (
              <a
                key={p.href}
                target="_blank"
                href={p.href}
                role="menuitem"
                className="flex items-center justify-between px-3 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
              >
                <span>{p.label}</span>
                <ExternalLink className="h-3.5 w-3.5 text-muted-foreground" />
              </a>
            ))}
          </div>
          <div className="border-t py-1">
            <button
              type="button"
              role="menuitem"
              onClick={onLogout}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-destructive hover:bg-accent"
            >
              <LogOut className="h-4 w-4" />
              Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
