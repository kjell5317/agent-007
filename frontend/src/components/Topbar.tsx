import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  CircleCheckBig,
  CircleUser,
  ExternalLink,
  LogOut,
  Search,
  Tags,
} from "lucide-react";
import type { ReactNode } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { subscribeEvents } from "@/lib/events";
import type { ThemePreference } from "@/lib/theme";
import { cn } from "@/lib/utils";

function formatPoints(n: number): string {
  // Points are whole numbers server-side; round defensively for display.
  return String(Math.round(n));
}

// Minimal invented points glyph — a four-point sparkle.
function PointsIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden
      className={className}
    >
      <path d="M12 2c.5 5.5 4 9 9.5 10C16 13 12.5 16.5 12 22c-.5-5.5-4-9-9.5-10C8 11 11.5 7.5 12 2Z" />
    </svg>
  );
}

export function Topbar({
  theme,
  onThemeChange,
  mode = "normal",
  title = "",
  onChatOpen,
  chatSearch,
  onPointsOpen,
  onLabelsOpen,
  onBack,
}: {
  theme: ThemePreference;
  onThemeChange: (next: ThemePreference) => void;
  mode?: "normal" | "chat" | "points" | "labels";
  title?: string;
  onChatOpen?: () => void;
  chatSearch?: ReactNode;
  onPointsOpen?: () => void;
  onLabelsOpen?: () => void;
  onBack?: () => void;
}) {
  const [email, setEmail] = useState<string | null>(null);
  const [autoPoll, setAutoPoll] = useState<boolean | null>(null);
  const [slackApps, setSlackApps] = useState<string[]>([]);
  const [points, setPoints] = useState<number | null>(null);
  // A short-lived "+N / −N" burst keyed by a counter so each change replays
  // the float + pop animation even when the same delta repeats.
  const [flash, setFlash] = useState<{ key: number; delta: number } | null>(
    null,
  );
  const prevPoints = useRef<number | null>(null);
  const flashSeq = useRef(0);
  const pointsRefreshInFlight = useRef(false);

  const refreshPoints = useCallback(
    async ({ clearOnError = false }: { clearOnError?: boolean } = {}) => {
      if (pointsRefreshInFlight.current) return;
      pointsRefreshInFlight.current = true;
      try {
        const r = await api.getPoints();
        setPoints(r.total);
        prevPoints.current = r.total;
      } catch {
        if (clearOnError && prevPoints.current == null) {
          setPoints(null);
        }
      } finally {
        pointsRefreshInFlight.current = false;
      }
    },
    [],
  );

  useEffect(() => {
    api
      .whoami()
      .then((r) => setEmail(r?.email ?? null))
      .catch(() => setEmail(null));
    api
      .getSettings()
      .then((s) => setAutoPoll(s.auto_poll_enabled))
      .catch(() => setAutoPoll(null));
    api
      .slackApps()
      .then(setSlackApps)
      .catch(() => setSlackApps([]));
  }, []);

  useEffect(() => {
    refreshPoints({ clearOnError: true });
  }, [refreshPoints]);

  useEffect(() => {
    let cancelled = false;

    const safeRefreshPoints = () => {
      if (!cancelled) void refreshPoints();
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") safeRefreshPoints();
    };

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", safeRefreshPoints);

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", safeRefreshPoints);
    };
  }, [refreshPoints]);

  // Live points: the backend pushes a `points` event whenever the total
  // changes (task crossed off, manual adjust, Home Assistant). Animate the
  // difference, but never on the very first value we learn.
  useEffect(() => {
    return subscribeEvents((event) => {
      if (event.type !== "points") return;
      const prev = prevPoints.current;
      prevPoints.current = event.total;
      setPoints(event.total);
      if (prev != null && event.total !== prev) {
        setFlash({ key: ++flashSeq.current, delta: event.total - prev });
      }
    });
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

  const logout = async () => {
    await api.logout();
    location.href = "/";
  };

  return (
    <header className="sticky top-0 z-30 bg-background/90 backdrop-blur-xl backdrop-saturate-150 supports-[backdrop-filter]:bg-background/75">
      <div className="mx-auto flex max-w-2xl items-center gap-2 px-4 py-3">
        {mode === "chat" || mode === "points" ? (
          <>
            <Button
              size="icon"
              variant="ghost"
              onClick={onBack}
              aria-label={mode === "chat" ? "Close search" : "Back"}
              className="h-12 w-12 shrink-0"
            >
              <ArrowLeft className="h-5 w-5" />
            </Button>
            {mode === "chat" ? chatSearch : (
              <button
                type="button"
                onClick={onChatOpen}
                aria-label="Search and chat"
                className="flex h-12 min-w-0 flex-1 items-center gap-3 rounded-full bg-secondary px-4 text-left text-sm text-muted-foreground shadow-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Search className="h-5 w-5 shrink-0" />
                <span className="truncate">Search...</span>
              </button>
            )}
          </>
        ) : mode !== "normal" ? (
          <>
            <Button
              size="icon"
              variant="ghost"
              onClick={onBack}
              aria-label="Back"
            >
              <ArrowLeft className="h-5 w-5" />
            </Button>
            <h1 className="flex-1 text-lg font-semibold">{title}</h1>
          </>
        ) : (
          <>
            <span className="flex h-12 w-12 shrink-0 items-center justify-center text-primary" aria-hidden="true">
              <CircleCheckBig className="h-6 w-6" />
            </span>
            <button
              type="button"
              onClick={onChatOpen}
              aria-label="Search and chat"
              className="flex h-12 min-w-0 flex-1 items-center gap-3 rounded-full bg-secondary px-4 text-left text-sm text-muted-foreground shadow-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Search className="h-5 w-5 shrink-0" />
              <span className="truncate">Search...</span>
            </button>
          </>
        )}
        {(mode === "normal" || mode === "chat" || mode === "points") && (
          <>
            <div className="relative shrink-0">
              <Button
                size="sm"
                variant="secondary"
                onClick={onPointsOpen}
                className={cn("h-12 w-14 gap-1 px-1 tabular-nums", mode === "points" && "bg-accent")}
                title={points == null ? "Points unavailable" : "Points"}
                aria-label={
                  points == null
                    ? "Points unavailable"
                    : `${formatPoints(points)} points`
                }
              >
                <PointsIcon className="h-3.5 w-3.5 text-amber-500" />
                <span
                  key={flash?.key ?? "idle"}
                  className={cn("inline-block", flash && "animate-points-pop")}
                >
                  {points == null ? "—" : formatPoints(points)}
                </span>
              </Button>
              {flash && (
                <span
                  key={flash.key}
                  onAnimationEnd={() => setFlash(null)}
                  className={cn(
                    "animate-points-float pointer-events-none absolute -top-2 left-1/2 text-xs font-bold tabular-nums",
                    flash.delta >= 0 ? "text-emerald-500" : "text-destructive",
                  )}
                >
                  {flash.delta >= 0 ? "+" : "−"}
                  {formatPoints(Math.abs(flash.delta))}
                </span>
              )}
            </div>
            <AccountMenu
              email={email}
              autoPoll={autoPoll}
              slackApps={slackApps}
              theme={theme}
              onToggleAutoPoll={toggleAutoPoll}
              onThemeChange={onThemeChange}
              onLabelsOpen={onLabelsOpen}
              onLogout={logout}
            />
          </>
        )}
      </div>
    </header>
  );
}

function AccountMenu({
  email,
  autoPoll,
  slackApps,
  theme,
  onToggleAutoPoll,
  onThemeChange,
  onLabelsOpen,
  onLogout,
}: {
  email: string | null;
  autoPoll: boolean | null;
  slackApps: string[];
  theme: ThemePreference;
  onToggleAutoPoll: (next: boolean) => void;
  onThemeChange: (next: ThemePreference) => void;
  onLabelsOpen?: () => void;
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
        className="h-12 w-12"
        onClick={() => setOpen((v) => !v)}
        aria-label="Account menu"
        aria-haspopup="menu"
        aria-expanded={open}
        title={email ?? "Account"}
      >
        <CircleUser className="h-5 w-5" />
      </Button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-50 mt-2 w-56 overflow-hidden rounded-md border bg-card text-card-foreground shadow-md"
        >
          <div className="truncate border-b px-3 py-2 text-xs text-muted-foreground">
            {email ?? "Account"}
          </div>
          <div className="py-1">
            <div className="px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Connect
            </div>
            {[
              { label: "Notion", href: "/oauth/notion/authorize" },
              ...slackApps.map((name) => ({
                label: `Slack · ${name}`,
                href: `/oauth/slack/authorize?app=${encodeURIComponent(name)}`,
              })),
            ].map((p) => (
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
            <div className="px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Preferences
            </div>
            {autoPoll !== null && (
              <label className="flex cursor-pointer items-center justify-between px-3 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground">
                <span>Auto sync · 5 min</span>
                <Switch
                  checked={autoPoll}
                  onChange={(e) => onToggleAutoPoll(e.target.checked)}
                />
              </label>
            )}
            <label className="flex cursor-pointer items-center justify-between px-3 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground">
              <span className="flex items-center gap-2">Dark mode</span>
              <Switch
                checked={theme === "dark"}
                onChange={(e) =>
                  onThemeChange(e.target.checked ? "dark" : "light")
                }
              />
            </label>
          </div>
          <div className="border-t py-1">
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onLabelsOpen?.();
              }}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-accent hover:text-accent-foreground"
            >
              <Tags className="h-4 w-4 text-muted-foreground" />
              Edit labels
            </button>
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
