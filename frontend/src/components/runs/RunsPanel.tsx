import { Box, ChevronDown, ChevronRight, GitBranch } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { RunCard, RunStatusBadge } from "@/components/runs/RunCard";
import { RunDocModal } from "@/components/runs/RunDocModal";
import { runTitle } from "@/components/runs/runLabels";
import { cn } from "@/lib/utils";
import type { RunsData } from "@/hooks/useRuns";
import { kotx, type KotxContainer, type KotxTask } from "@/lib/kotx";

interface RunGroup {
  key: string;
  repo: string;
  branch: string | null;
  tasks: KotxTask[];
}

function isActionable(task: KotxTask): boolean {
  return task.canStart || task.canApprove || task.canComment;
}

// Float tasks that need action to the top, keeping the original order within
// each bucket (stable). Sorting before grouping lifts whole repo+branch groups
// that contain something actionable, and orders runs within a group too.
function sortActionableFirst(tasks: KotxTask[]): KotxTask[] {
  return [...tasks].sort(
    (a, b) => Number(isActionable(b)) - Number(isActionable(a)),
  );
}

// Group runs that share a repo + branch, preserving the incoming order by the
// position of each group's first run. Runs without a branch can't share one, so
// each stands alone.
function groupRuns(tasks: KotxTask[]): RunGroup[] {
  const groups = new Map<string, RunGroup>();
  const order: string[] = [];
  for (const task of tasks) {
    const key = task.branch ? `b:${task.repo}\u0000${task.branch}` : `s:${task.id}`;
    let group = groups.get(key);
    if (!group) {
      group = { key, repo: task.repo, branch: task.branch, tasks: [] };
      groups.set(key, group);
      order.push(key);
    }
    group.tasks.push(task);
  }
  return order.map((key) => groups.get(key)!);
}

interface Props extends RunsData {
  selectedRunId: number | null;
  onRunOpen: (id: number) => void;
  onSelectedRunClose: () => void;
}

function runCountLabel(count: number): string {
  return `${count} ${count === 1 ? "run" : "runs"}`;
}

function primaryDoc(task: KotxTask): "task" | "review" {
  return task.kind === "review" ? "review" : "task";
}

export function RunsPanel({
  tasks,
  containers,
  loading,
  error,
  scope,
  setScope,
  refresh,
  selectedRunId,
  onRunOpen,
  onSelectedRunClose,
}: Props) {
  const [showContainers, setShowContainers] = useState(false);
  const [fetchedRun, setFetchedRun] = useState<KotxTask | null>(null);
  const selectedListRun = useMemo(
    () => tasks.find((task) => task.id === selectedRunId) ?? null,
    [selectedRunId, tasks],
  );
  const selectedRun = selectedRunId ? selectedListRun ?? fetchedRun : null;

  useEffect(() => {
    if (!selectedRunId || selectedListRun) {
      setFetchedRun(null);
      return;
    }
    let cancelled = false;
    kotx
      .getTask(selectedRunId)
      .then((task) => {
        if (!cancelled) setFetchedRun(task);
      })
      .catch(() => {
        if (!cancelled) onSelectedRunClose();
      });
    return () => {
      cancelled = true;
    };
  }, [onSelectedRunClose, selectedListRun, selectedRunId]);

  if (error) {
    return (
      <>
        <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
          Couldn't reach the runs API.
          <div className="mt-1 text-xs">{error}</div>
        </div>
        {selectedRun && (
          <RunDocModal
            task={selectedRun}
            doc={primaryDoc(selectedRun)}
            onClose={onSelectedRunClose}
            onChanged={refresh}
          />
        )}
      </>
    );
  }

  return (
    <>
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="inline-flex rounded-lg bg-muted p-0.5 text-sm">
            {(["active", "all"] as const).map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setScope(s)}
                className={cn(
                  "rounded-md px-3 py-1 font-medium capitalize transition-colors",
                  scope === s
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {s}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => setShowContainers((v) => !v)}
            className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
          >
            {showContainers ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
            <Box className="h-3.5 w-3.5" />
            {containers.length} running
          </button>
        </div>

        <Collapsible open={showContainers}>
          <div className="space-y-2">
            {containers.length === 0 ? (
              <p className="px-1 text-xs text-muted-foreground">No containers running.</p>
            ) : (
              containers.map((c) => <ContainerRow key={c.id} container={c} />)
            )}
          </div>
        </Collapsible>

        {loading && tasks.length === 0 ? (
          <div className="py-8 text-center text-sm text-muted-foreground">Loading…</div>
        ) : tasks.length === 0 ? (
          <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
            No {scope === "active" ? "active " : ""}runs.
          </div>
        ) : (
          <RunList
            groups={groupRuns(sortActionableFirst(tasks))}
            scope={scope}
            refresh={refresh}
            onRunOpen={onRunOpen}
          />
        )}
      </div>
      {selectedRun && (
        <RunDocModal
          task={selectedRun}
          doc={primaryDoc(selectedRun)}
          onClose={onSelectedRunClose}
          onChanged={refresh}
        />
      )}
    </>
  );
}

function RunList({
  groups,
  scope,
  refresh,
  onRunOpen,
}: {
  groups: RunGroup[];
  scope: "active" | "all";
  refresh: () => Promise<void> | void;
  onRunOpen: (id: number) => void;
}) {
  if (scope === "all") {
    return (
      <div className="space-y-2">
        {groups.map((group) => (
          <AllRunGroup
            key={group.key}
            group={group}
            refresh={refresh}
            onRunOpen={onRunOpen}
          />
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {groups.map((group) =>
        group.tasks.length === 1 ? (
          <RunCard
            key={group.tasks[0].id}
            task={group.tasks[0]}
            onChanged={refresh}
            onOpen={onRunOpen}
          />
        ) : (
          <RunGroup
            key={group.key}
            group={group}
            refresh={refresh}
            onRunOpen={onRunOpen}
          />
        ),
      )}
    </div>
  );
}

function AllRunGroup({
  group,
  refresh,
  onRunOpen,
}: {
  group: RunGroup;
  refresh: () => Promise<void> | void;
  onRunOpen: (id: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const hasMultipleRuns = group.tasks.length > 1;
  const title = group.branch ?? runTitle(group.tasks[0]);
  const Chevron = hasMultipleRuns && open ? ChevronDown : ChevronRight;

  return (
    <Card>
      <CardContent
        className="cursor-pointer"
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("button,a,summary")) return;
          if (hasMultipleRuns) {
            setOpen((v) => !v);
          } else {
            onRunOpen(group.tasks[0].id);
          }
        }}
      >
        <div className="flex items-center gap-2">
          <div className="h-8 w-8 shrink-0" />

          <div className="min-w-0 flex-1">
            <div className="min-w-0 truncate font-medium leading-snug" title={title}>
              {title}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
              <span className="truncate font-medium" title={group.repo}>
                {group.repo}
              </span>
              <MetaDot />
              <GroupStatusBadge group={group} />
              <MetaDot />
              <span className="font-medium">{runCountLabel(group.tasks.length)}</span>
            </div>
          </div>

          <Chevron className="h-4 w-4 shrink-0 text-muted-foreground" />
        </div>

        {hasMultipleRuns && (
          <Collapsible open={open}>
            <div
              className="mt-3 space-y-2 border-t pt-2"
              onClick={(e) => e.stopPropagation()}
            >
              {group.tasks.map((task) => (
                <RunCard
                  key={task.id}
                  task={task}
                  onChanged={refresh}
                  onOpen={onRunOpen}
                  displayMode="readonly"
                />
              ))}
            </div>
          </Collapsible>
        )}
      </CardContent>
    </Card>
  );
}

function GroupStatusBadge({ group }: { group: RunGroup }) {
  const statuses = new Set(group.tasks.map((task) => task.status));
  if (statuses.size === 1) {
    return <RunStatusBadge task={group.tasks[0]} />;
  }
  return <Badge variant="muted">{statuses.size} statuses</Badge>;
}

function MetaDot() {
  return (
    <span aria-hidden className="text-muted-foreground">
      •
    </span>
  );
}

function RunGroup({
  group,
  refresh,
  onRunOpen,
}: {
  group: RunGroup;
  refresh: () => Promise<void> | void;
  onRunOpen: (id: number) => void;
}) {
  const [open, setOpen] = useState(true);
  return (
    <div className="rounded-xl border bg-muted/20">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-xs text-muted-foreground"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0" />
        )}
        <span className="min-w-0 shrink truncate font-medium text-foreground">{group.repo}</span>
        <span className="flex min-w-0 shrink items-center gap-1">
          <GitBranch className="h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0 shrink truncate font-mono">{group.branch}</span>
        </span>
        <span className="ml-auto shrink-0 pl-2 tabular-nums">
          {runCountLabel(group.tasks.length)}
        </span>
      </button>
      <Collapsible open={open}>
        <div className="space-y-2 p-2 pt-0">
          {group.tasks.map((t) => (
            <RunCard key={t.id} task={t} onChanged={refresh} onOpen={onRunOpen} />
          ))}
        </div>
      </Collapsible>
    </div>
  );
}
function ContainerRow({ container }: { container: KotxContainer }) {
  return (
    <Card>
      <CardContent className="flex items-center gap-2 p-2.5 pl-3">
        <span className="inline-block h-2 w-2 shrink-0 rounded-full bg-emerald-500" />
        <span className="min-w-0 flex-1 truncate font-mono text-xs">{container.name}</span>
        <span className="shrink-0 text-xs text-muted-foreground">{container.status}</span>
      </CardContent>
    </Card>
  );
}
