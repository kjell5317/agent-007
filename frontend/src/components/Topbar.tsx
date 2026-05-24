import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  onSynced: () => Promise<void> | void;
}

const SOURCES = ["gmail", "slack"] as const;

export function Topbar({ onSynced }: Props) {
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [email, setEmail] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);

  useEffect(() => {
    api.health().then(() => setHealthy(true)).catch(() => setHealthy(false));
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
            healthy == null ? "bg-muted-foreground" : healthy ? "bg-emerald-500" : "bg-destructive",
          )}
          title={healthy == null ? "checking" : healthy ? "healthy" : "unreachable"}
        />
        <h1 className="flex-1 text-base font-semibold">Task Agent</h1>
        <Button size="sm" variant="outline" onClick={sync} disabled={syncing}>
          <RefreshCw className={cn("h-4 w-4", syncing && "animate-spin")} />
          Sync
        </Button>
        {email && (
          <Button size="sm" variant="ghost" onClick={logout}>
            Logout
          </Button>
        )}
      </div>
    </header>
  );
}
