import { useCallback, useEffect, useRef, useState } from "react";
import { CircleUser, ExternalLink, LogOut, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  onSynced: () => Promise<void> | void;
}

// Each entry becomes a "Connect <label>" link inside the account dropdown.
// The href points at the backend's generic OAuth authorize route, which
// redirects to the provider's consent screen and back through /oauth/<p>/callback.
// Google is intentionally omitted: /auth/login already captures Gmail +
// Calendar scopes alongside the session, so a separate entry would duplicate it.
const OAUTH_PROVIDERS: { label: string; href: string }[] = [
  { label: "CSEE", href: "/oauth/slack/authorize?app=csee" },
  { label: "Social AI", href: "/oauth/slack/authorize?app=social" },
];

export function Topbar({ onSynced }: Props) {
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [email, setEmail] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [autoPoll, setAutoPoll] = useState<boolean | null>(null);

  useEffect(() => {
    api
      .health()
      .then(() => setHealthy(true))
      .catch(() => setHealthy(false));
    api
      .whoami()
      .then((r) => setEmail(r?.email ?? null))
      .catch(() => setEmail(null));
    api
      .getSettings()
      .then((s) => setAutoPoll(s.auto_poll_enabled))
      .catch(() => setAutoPoll(null));
  }, []);

  const toggleAutoPoll = useCallback(
    async (next: boolean) => {
      const prev = autoPoll;
      setAutoPoll(next);
      try {
        const updated = await api.updateSettings({ auto_poll_enabled: next });
        setAutoPoll(updated.auto_poll_enabled);
      } catch (err) {
        setAutoPoll(prev);
        toast.error(`Failed to update setting: ${(err as Error).message}`);
      }
    },
    [autoPoll],
  );

  const sync = async () => {
    setSyncing(true);
    const toastId = toast.loading("Syncing…");
    try {
      const result = await api.poll();
      const parts = [
        `${result.fetched} fetched`,
        `${result.tasks_created} new task${result.tasks_created === 1 ? "" : "s"}`,
      ];
      if (result.errors.length) parts.push(`${result.errors.length} error(s)`);
      toast.success(`Synced: ${parts.join(" · ")}`, { id: toastId });
      await onSynced();
    } catch (err) {
      toast.error(`Sync failed: ${(err as Error).message}`, { id: toastId });
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
        {email && (
          <AccountMenu
            email={email}
            autoPoll={autoPoll}
            onToggleAutoPoll={toggleAutoPoll}
            onLogout={logout}
          />
        )}
      </div>
    </header>
  );
}

function AccountMenu({
  email,
  autoPoll,
  onToggleAutoPoll,
  onLogout,
}: {
  email: string;
  autoPoll: boolean | null;
  onToggleAutoPoll: (next: boolean) => void;
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
          {autoPoll !== null && (
            <div className="border-t py-1">
              <div className="px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                Preferences
              </div>
              <label className="flex cursor-pointer items-center justify-between px-3 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground">
                <span>Auto sync · 5 min</span>
                <Switch
                  checked={autoPoll}
                  onChange={(e) => onToggleAutoPoll(e.target.checked)}
                />
              </label>
            </div>
          )}
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

function Switch({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
    <span className="relative inline-flex h-5 w-9 shrink-0 items-center">
      <input
        type="checkbox"
        role="switch"
        aria-checked={checked}
        checked={checked}
        onChange={onChange}
        className="peer h-full w-full cursor-pointer appearance-none rounded-full bg-muted transition-colors checked:bg-emerald-500"
      />
      <span
        aria-hidden
        className={cn(
          "pointer-events-none absolute left-0.5 h-4 w-4 rounded-full bg-card shadow transition-transform",
          checked && "translate-x-4",
        )}
      />
    </span>
  );
}
