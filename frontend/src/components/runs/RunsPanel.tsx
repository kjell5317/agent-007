import { Box, ChevronDown, ChevronRight, Circle, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { IconAction, RunCard, RunStatusBadge } from "@/components/runs/RunCard";
import { RunDocModal } from "@/components/runs/RunDocModal";
import {
  actionHint,
  isMergeProposal,
  runStatusLabel,
  runTitle,
} from "@/components/runs/runLabels";
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
  return task.canStart || task.canApprove || task.canComment || isMergeProposal(task);
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

function timestamp(value: string | null | undefined): number | null {
  if (!value) return null;
  const time = Date.parse(value);
  return Number.isNaN(time) ? null : time;
}

function newestRun(tasks: KotxTask[]): KotxTask {
  return tasks.reduce((newest, task) => {
    const newestCreatedAt = timestamp(newest.createdAt);
    const taskCreatedAt = timestamp(task.createdAt);
    if (newestCreatedAt !== taskCreatedAt) {
      return (taskCreatedAt ?? Number.NEGATIVE_INFINITY) >
        (newestCreatedAt ?? Number.NEGATIVE_INFINITY)
        ? task
        : newest;
    }

    const newestUpdatedAt = timestamp(newest.updatedAt);
    const taskUpdatedAt = timestamp(task.updatedAt);
    if (newestUpdatedAt !== taskUpdatedAt) {
      return (taskUpdatedAt ?? Number.NEGATIVE_INFINITY) >
        (newestUpdatedAt ?? Number.NEGATIVE_INFINITY)
        ? task
        : newest;
    }

    return task.id > newest.id ? task : newest;
  });
}

interface Props extends RunsData {
  selectedRunId: number | null;
  onRunOpen: (id: number) => void;
  onSelectedRunClose: () => void;
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
  const selectedRunSiblings = useMemo(() => {
    if (!selectedRun?.branch) return [];
    return tasks.filter(
      (task) =>
        task.id !== selectedRun.id &&
        task.repo === selectedRun.repo &&
        task.branch === selectedRun.branch,
    );
  }, [selectedRun, tasks]);

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
            sameBranchTasks={selectedRunSiblings}
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
          <RunListSkeleton />
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
          sameBranchTasks={selectedRunSiblings}
          onClose={onSelectedRunClose}
          onChanged={refresh}
        />
      )}
    </>
  );
}

function RunListSkeleton() {
  return (
    <div className="space-y-2">
      {[0, 1, 2].map((i) => (
        <Card key={i}>
          <CardContent className="flex items-center gap-2 p-3">
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              <SkeletonBlock className="h-4 w-3/5" />
              <div className="flex items-center gap-2">
                <SkeletonBlock className="h-4 w-16 rounded-full" />
                <SkeletonBlock className="h-3 w-24" />
              </div>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
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
  // A lone active run stays a full, actionable card; every grouped case (and
  // the read-only "all" scope) uses the compact inbox-style group card.
  if (scope === "all") {
    return (
      <div className="space-y-2">
        {groups.map((group) => (
          <RunGroupCard
            key={group.key}
            group={group}
            scope={scope}
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
          <RunGroupCard
            key={group.key}
            group={group}
            scope={scope}
            refresh={refresh}
            onRunOpen={onRunOpen}
          />
        ),
      )}
    </div>
  );
}

// Mirrors the inbox group card: a status badge + repo header line with the runs
// collapsed underneath as compact title/status rows. A single-run group drops
// the count and the expander and just opens the run on click.
function RunGroupCard({
  group,
  scope,
  refresh,
  onRunOpen,
}: {
  group: RunGroup;
  scope: "active" | "all";
  refresh: () => Promise<void> | void;
  onRunOpen: (id: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const hasMultipleRuns = group.tasks.length > 1;
  const hasPrimaryAction = group.tasks.some((task) => actionHint(task) !== null);
  const title = hasMultipleRuns
    ? group.branch ?? runTitle(group.tasks[0])
    : runTitle(group.tasks[0]);
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <Card>
      <CardContent
        className={cn("cursor-pointer", scope === "active" ? "pl-3" : "pl-4")}
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
          {scope === "active" && (
            <div className="flex h-8 w-8 shrink-0 items-center justify-center text-muted-foreground">
              <Circle className="h-5 w-5" />
            </div>
          )}
          <div className="min-w-0 flex-1">
            <div className="min-w-0 truncate font-medium leading-snug" title={title}>
              {title}
            </div>
            <div className="mt-1 flex items-center gap-x-2 text-xs text-muted-foreground">
              <span className="shrink-0">
                <GroupStatusBadge group={group} scope={scope} />
              </span>
              <span className="min-w-0 truncate font-medium" title={group.repo}>
                {group.repo}
              </span>
              {hasMultipleRuns && (
                <span className="flex shrink-0 items-center gap-x-2">
                  <MetaDot />
                  <span className="font-medium">{group.tasks.length} runs</span>
                </span>
              )}
            </div>
          </div>

          {hasMultipleRuns &&
            (hasPrimaryAction ? (
              <Button
                type="button"
                size="icon"
                className="h-8 w-8 shrink-0"
                onClick={(e) => {
                  e.stopPropagation();
                  setOpen((v) => !v);
                }}
                title={open ? "Collapse group" : "Expand group"}
                aria-label={open ? "Collapse group" : "Expand group"}
              >
                <Chevron className="h-4 w-4" />
              </Button>
            ) : (
              <Chevron className="h-4 w-4 shrink-0 text-muted-foreground" />
            ))}
        </div>

        {hasMultipleRuns && (
          <Collapsible open={open}>
            <div
              className="mt-3 space-y-1 border-t pt-2"
              onClick={(e) => e.stopPropagation()}
            >
              {group.tasks.map((task) => (
                <RunGroupMember
                  key={task.id}
                  task={task}
                  onOpen={onRunOpen}
                  onChanged={refresh}
                  showDiscard={scope === "active"}
                />
              ))}
            </div>
          </Collapsible>
        )}
      </CardContent>
    </Card>
  );
}

function RunGroupMember({
  task,
  onOpen,
  onChanged,
  showDiscard,
}: {
  task: KotxTask;
  onOpen: (id: number) => void;
  onChanged: () => Promise<void> | void;
  showDiscard: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const title = runTitle(task);
  const hint = actionHint(task);

  const discard = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setBusy(true);
    try {
      await kotx.discard(task.id);
      toast.success("Discarded");
      await onChanged();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-w-0 items-center gap-1 rounded-md border bg-muted/30 pr-2">
      {showDiscard &&
        (task.canDiscard ? (
          <IconAction onClick={discard} disabled={busy} title="Discard task">
            <Trash2 className="h-4 w-4" />
          </IconAction>
        ) : (
          <div className="h-8 w-8 shrink-0" />
        ))}
      <button
        type="button"
        onClick={() => onOpen(task.id)}
        className={cn(
          "flex min-w-0 flex-1 items-center gap-2 py-1.5 text-left",
          !showDiscard && "pl-2",
        )}
      >
        <span className="min-w-0 flex-1 truncate text-sm" title={title}>
          {title}
        </span>
        <span className="shrink-0">
          <RunStatusBadge task={task} />
        </span>
      </button>
      {hint && (
        <Button
          type="button"
          size="sm"
          className="h-7 shrink-0 px-2 text-xs"
          onClick={() => onOpen(task.id)}
          disabled={busy}
        >
          {hint}
        </Button>
      )}
    </div>
  );
}

function GroupStatusBadge({ group, scope }: { group: RunGroup; scope: "active" | "all" }) {
  const statuses = new Set(group.tasks.map((task) => runStatusLabel(task)));
  if (statuses.size === 1) {
    return <RunStatusBadge task={group.tasks[0]} />;
  }
  if (scope === "active") {
    return <RunStatusBadge task={newestRun(group.tasks)} />;
  }
  return <Badge variant="muted">mixed</Badge>;
}

function MetaDot() {
  return (
    <span aria-hidden className="text-muted-foreground">
      •
    </span>
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
