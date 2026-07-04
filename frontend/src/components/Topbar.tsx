import { useCallback, useEffect, useRef, useState } from "react";
import { CircleUser, ExternalLink, LogOut, Mail } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, type PointsLogEntry } from "@/lib/api";
import { subscribeEvents } from "@/lib/events";
import type { ThemePreference } from "@/lib/theme";
import { cn } from "@/lib/utils";

// Each entry becomes a "Connect <label>" link inside the account dropdown.
// The href points at the backend's generic OAuth authorize route, which
// redirects to the provider's consent screen and back through /oauth/<p>/callback.
// Google is intentionally omitted: /auth/login already captures Gmail +
// Calendar scopes alongside the session, so a separate entry would duplicate it.
const OAUTH_PROVIDERS: { label: string; href: string }[] = [
  { label: "CSEE", href: "/oauth/slack/authorize?app=csee" },
  { label: "Social AI", href: "/oauth/slack/authorize?app=social" },
];

function formatPoints(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

function formatSignedPoints(n: number): string {
  if (n === 0) return formatPoints(0);
  return `${n > 0 ? "+" : "−"}${formatPoints(Math.abs(n))}`;
}

function formatLogTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
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
  unreadInbox = 0,
  onMailOpen,
  onBack,
}: {
  theme: ThemePreference;
  onThemeChange: (next: ThemePreference) => void;
  mode?: "normal" | "mail";
  unreadInbox?: number;
  onMailOpen?: () => void;
  onBack?: () => void;
}) {
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [email, setEmail] = useState<string | null>(null);
  const [autoPoll, setAutoPoll] = useState<boolean | null>(null);
  const [points, setPoints] = useState<number | null>(null);
  const [pointsOpen, setPointsOpen] = useState(false);
  // A short-lived "+N / −N" burst keyed by a counter so each change replays
  // the float + pop animation even when the same delta repeats.
  const [flash, setFlash] = useState<{ key: number; delta: number } | null>(
    null,
  );
  const prevPoints = useRef<number | null>(null);
  const flashSeq = useRef(0);

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
    api
      .getPoints()
      .then((r) => {
        setPoints(r.total);
        prevPoints.current = r.total;
      })
      .catch(() => setPoints(null));
  }, []);

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
        {mode === "mail" ? (
          <Button onClick={onBack}>Back</Button>
        ) : (
          <>
            {points != null && (
              <div className="relative">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setPointsOpen(true)}
                  className="gap-1.5 tabular-nums"
                  title="Adjust points"
                >
                  <PointsIcon className="h-3.5 w-3.5 text-amber-500" />
                  <span
                    key={flash?.key ?? "idle"}
                    className={cn(
                      "inline-block",
                      flash && "animate-points-pop",
                    )}
                  >
                    {formatPoints(points)}
                  </span>
                </Button>
                {flash && (
                  <span
                    key={flash.key}
                    onAnimationEnd={() => setFlash(null)}
                    className={cn(
                      "animate-points-float pointer-events-none absolute -top-2 left-1/2 text-xs font-bold tabular-nums",
                      flash.delta >= 0
                        ? "text-emerald-500"
                        : "text-destructive",
                    )}
                  >
                    {flash.delta >= 0 ? "+" : "−"}
                    {formatPoints(Math.abs(flash.delta))}
                  </span>
                )}
              </div>
            )}
            <Button
              size="icon"
              variant="ghost"
              onClick={onMailOpen}
              aria-label={
                unreadInbox > 0
                  ? `Open mail, ${unreadInbox} unread`
                  : "Open mail"
              }
              title="Mail"
              className="relative"
            >
              <Mail className="h-5 w-5" />
              {unreadInbox > 0 && (
                <span className="absolute right-1.5 top-1.5 h-2.5 w-2.5 rounded-full border-2 border-card bg-emerald-500" />
              )}
            </Button>
            {email && (
              <AccountMenu
                email={email}
                autoPoll={autoPoll}
                theme={theme}
                onToggleAutoPoll={toggleAutoPoll}
                onThemeChange={onThemeChange}
                onLogout={logout}
              />
            )}
          </>
        )}
      </div>
      <PointsModal
        open={pointsOpen}
        onClose={() => setPointsOpen(false)}
        total={points}
        onTotal={setPoints}
      />
    </header>
  );
}

function PointsModal({
  open,
  onClose,
  total,
  onTotal,
}: {
  open: boolean;
  onClose: () => void;
  total: number | null;
  onTotal: (total: number) => void;
}) {
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [tab, setTab] = useState("adjust");
  const [logEntries, setLogEntries] = useState<PointsLogEntry[]>([]);
  const [logCount, setLogCount] = useState(0);
  const [logSeenBefore, setLogSeenBefore] = useState<string | null>(null);
  const [logLoaded, setLogLoaded] = useState(false);
  const [logBusy, setLogBusy] = useState(false);
  const [busy, setBusy] = useState(false);

  const resetLog = () => {
    setTab("adjust");
    setLogEntries([]);
    setLogCount(0);
    setLogSeenBefore(null);
    setLogLoaded(false);
    setLogBusy(false);
  };

  const close = () => {
    resetLog();
    onClose();
  };

  // Load the log (entries + unseen count + watermark) as soon as the modal
  // opens, so the Log tab's unseen badge is populated before that tab is
  // opened — the same way the inbox fills its unread badge before you enter
  // the mail view. Don't rely on finally() to clear busy: setLogLoaded
  // re-runs this effect and its cleanup flips `cancelled`, so a deferred
  // reset would be skipped and the spinner would hang.
  useEffect(() => {
    if (!open || logLoaded) return;
    let cancelled = false;
    setLogBusy(true);
    api
      .getPointsLog()
      .then((res) => {
        if (cancelled) return;
        setLogEntries(res.entries);
        setLogCount(res.count);
        setLogSeenBefore(res.last_seen_at);
        setLogLoaded(true);
        setLogBusy(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setLogBusy(false);
        toast.error(`Failed to load log: ${(err as Error).message}`);
      });
    return () => {
      cancelled = true;
    };
  }, [logLoaded, open]);

  // Switching to the Log tab counts as viewing it: clear the unseen badge and
  // advance the server watermark, mirroring how opening the inbox resets its
  // unread count. The green per-entry dots stay (they key off the watermark
  // fetched on open) so you can still see what's new for this viewing.
  useEffect(() => {
    if (!open || tab !== "log" || logCount === 0) return;
    setLogCount(0);
    void api.markPointsLogSeen();
  }, [logCount, open, tab]);

  const apply = async (sign: 1 | -1) => {
    const magnitude = Math.abs(Number(amount));
    if (!Number.isFinite(magnitude) || magnitude === 0) {
      toast.error("Enter a non-zero amount.");
      return;
    }
    const delta = sign * magnitude;
    setBusy(true);
    try {
      const cleanReason = reason.trim();
      const res = await api.adjustPoints(delta, {
        caller: "Manual",
        ...(cleanReason ? { reason: cleanReason } : {}),
      });
      onTotal(res.total);
      const s = delta >= 0 ? "+" : "";
      toast.success(`${s}${formatPoints(delta)} points`);
      setAmount("");
      setReason("");
      close();
    } catch (err) {
      toast.error(`Failed: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={close}
      title="Adjust points"
      className="max-w-md"
    >
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList className="mb-4 grid w-full grid-cols-2">
          <TabsTrigger value="adjust">Adjust</TabsTrigger>
          <TabsTrigger value="log">
            Log
            {logCount > 0 && (
              <span className="ml-2 rounded-full bg-primary px-1.5 py-0.5 text-[10px] leading-none text-primary-foreground">
                {logCount}
              </span>
            )}
          </TabsTrigger>
        </TabsList>
        {/* Fixed-height body so switching tabs keeps the modal the same size
            (the Adjust form is short; the Log list is tall and variable).
            Flex column + overflow-hidden so the log scrolls inside the box
            instead of overflowing it — a percentage-height chain through
            Radix's TabsContent doesn't reliably clip.
            `data-[state=active]:flex` (not a bare `flex`): Radix keeps the
            inactive panel mounted with the `hidden` attribute, and a bare
            `flex` utility would override `[hidden]{display:none}`, leaving the
            hidden panel taking flex space. Gating the display on the active
            state lets `hidden` win for the inactive panel. */}
        <div className="flex h-80 flex-col overflow-hidden">
          <TabsContent
            value="adjust"
            className="mt-0 flex-1 flex-col justify-center data-[state=active]:flex"
          >
            {total != null && (
              <div className="mb-4 text-center">
                <span className="text-4xl font-bold tabular-nums">
                  {formatPoints(total)}
                </span>
                <span className="ml-1 text-sm text-muted-foreground">
                  points
                </span>
              </div>
            )}
            <Input
              type="number"
              inputMode="decimal"
              min="0"
              step="any"
              autoFocus
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              placeholder="Amount"
              aria-label="Amount"
              className="mb-3"
            />
            <Input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              maxLength={128}
              placeholder="Reason"
              aria-label="Reason"
              className="mb-3"
            />
            <div className="flex gap-2">
              <Button
                variant="outline"
                className="flex-1"
                disabled={busy}
                onClick={() => apply(-1)}
              >
                − Subtract
              </Button>
              <Button
                className="flex-1"
                disabled={busy}
                onClick={() => apply(1)}
              >
                + Add
              </Button>
            </div>
          </TabsContent>
          <TabsContent
            value="log"
            className="mt-0 min-h-0 flex-1 flex-col data-[state=active]:flex"
          >
            <div
              className={cn(
                "min-h-0 flex-1 overflow-y-auto",
                logEntries.length > 0 && "[scrollbar-gutter:stable]",
              )}
            >
              {logBusy ? (
                <div className="py-8 text-center text-sm text-muted-foreground">
                  Loading...
                </div>
              ) : logEntries.length === 0 ? (
                <div className="py-8 text-center text-sm text-muted-foreground">
                  No point changes yet.
                </div>
              ) : (
                <div className="divide-y pr-4">
                  {logEntries.map((entry) => (
                    <div
                      key={entry.id}
                      className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 py-3"
                    >
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          {logSeenBefore != null &&
                            new Date(entry.created_at).getTime() >
                              new Date(logSeenBefore).getTime() && (
                              <span
                                aria-label="Unseen"
                                title="Unseen"
                                className="inline-block h-2 w-2 shrink-0 rounded-full bg-emerald-500"
                              />
                            )}
                          <div className="min-w-0 flex-1 truncate text-sm font-medium">
                            {entry.reason}
                          </div>
                        </div>
                        <div className="mt-0.5 flex min-w-0 gap-2 text-xs text-muted-foreground">
                          <span className="shrink-0">
                            {formatLogTime(entry.created_at)}
                          </span>
                          {entry.caller && (
                            <span className="min-w-0 truncate">
                              {entry.caller}
                            </span>
                          )}
                        </div>
                      </div>
                      <div
                        className={cn(
                          "self-center text-sm font-semibold tabular-nums",
                          entry.amount >= 0
                            ? "text-emerald-500"
                            : "text-destructive",
                        )}
                      >
                        {formatSignedPoints(entry.amount)}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </TabsContent>
        </div>
      </Tabs>
    </Modal>
  );
}

function AccountMenu({
  email,
  autoPoll,
  theme,
  onToggleAutoPoll,
  onThemeChange,
  onLogout,
}: {
  email: string;
  autoPoll: boolean | null;
  theme: ThemePreference;
  onToggleAutoPoll: (next: boolean) => void;
  onThemeChange: (next: ThemePreference) => void;
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
