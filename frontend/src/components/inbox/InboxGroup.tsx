import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  CirclePlus,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import {
  ActionButton,
  hasInputDetails,
  InputBody,
  MetaDot,
} from "@/components/inbox/InboxCard";
import { InputStatusBadge } from "@/components/runs/RunStatusBadge";
import { useInboxActions } from "@/components/inbox/useInboxActions";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import {
  activeKotxRun,
  isAgentTaskFollowup,
  isDismissibleKotxRun,
  isKotxRun,
  senderName,
  type InboxGroup as GroupData,
} from "@/lib/inbox";
import { cn } from "@/lib/utils";
import type { RawInput } from "@/lib/types";

interface Props {
  group: GroupData;
  onChanged: () => Promise<void> | void;
  unseenMemberIds: string[];
  onVisible: (ids: string[]) => void;
  onOpenTask: (id: string) => void;
}

export function InboxGroup({
  group,
  onChanged,
  unseenMemberIds,
  onVisible,
  onOpenTask,
}: Props) {
  const [open, setOpen] = useState(false);
  const cardRef = useRef<HTMLDivElement>(null);
  const { busy, runTaskAction, promote, reopenTask, dismissRun } =
    useInboxActions(onChanged);

  const { members, newest, liveTask, closedTask } = group;
  const unseenMemberKey = useMemo(
    () => unseenMemberIds.join("\u0000"),
    [unseenMemberIds],
  );
  // Header shows the task's *status* (a no_change / duplicate follow-up on a
  // closed task is still a closed task) — but while a kotx run is in flight
  // (e.g. a resolve-conflict run on the task's PR) its live state takes over,
  // so the group adapts instead of sitting on "open". Each member below shows
  // its own outcome badge; groups with no task fall back to the newest member.
  const activeRun = activeKotxRun(members);
  const taskBadge = liveTask ? "open" : closedTask ? "closed" : null;

  const senders = Array.from(new Set(members.map(senderName)));
  const sendersLabel =
    senders.length === 1 ? senders[0] : `${senders[0]} +${senders.length - 1}`;

  const unread = unseenMemberIds.length > 0;
  const hasKotxRun = members.some(isKotxRun);
  const cardBorderClass = hasKotxRun
    ? "border-primary/50"
    : unread
      ? "border-emerald-500/70"
      : null;

  useEffect(() => {
    if (!unread) return;
    const node = cardRef.current;
    if (!node) return;

    if (typeof IntersectionObserver === "undefined") {
      onVisible(unseenMemberIds);
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        onVisible(unseenMemberIds);
        observer.disconnect();
      },
      { threshold: 0.5 },
    );

    observer.observe(node);
    return () => observer.disconnect();
  }, [onVisible, unread, unseenMemberIds, unseenMemberKey]);

  // Group-level action mirrors a single card, but "Make a task" feeds the
  // whole thread's context into extraction (anchored on the newest message).
  // Offer it when the thread has no live/closed task — embedding auto-decided
  // duplicates stay overridable this way — but not when the agent acted on an
  // existing task from a follow-up (reopened / updated / closed / no_change):
  // that task is real, so promoting would duplicate it.
  const agentActed = members.some(isAgentTaskFollowup);
  // A task-less thread whose members are all kotx transitions is a kotx run
  // (or successive runs on the same issue) that hasn't produced a task yet.
  // Offer "Dismiss run" (discard the newest run upstream) instead of "Make a
  // task from thread"; a run already terminal gets no action. Mixed
  // gmail+kotx github threads keep the promote path.
  const kotxRunThread = !liveTask && !closedTask && members.every(isKotxRun);
  const action = kotxRunThread
    ? isDismissibleKotxRun(newest)
      ? {
          label: "Dismiss run",
          Icon: Trash2,
          run: () => dismissRun(newest.id),
        }
      : null
    : !liveTask && !closedTask && !agentActed
      ? {
          label: "Make a task from thread",
          Icon: CirclePlus,
          run: () =>
            promote(newest.id, { contextInputIds: members.map((m) => m.id) }),
        }
      : liveTask
        ? {
            label: "Dismiss task",
            Icon: Trash2,
            run: () =>
              runTaskAction(
                liveTask.task_id!,
                api.markNotTask,
                "Task dismissed",
              ),
          }
        : closedTask
          ? {
              label: "Re-open task",
              Icon: RotateCcw,
              run: () => reopenTask(closedTask.task_id!),
            }
          : null;

  const Chevron = open ? ChevronDown : ChevronRight;

  // Clicking the card opens the group's task modal; expansion lives on the
  // chevron. Task-less groups do nothing.
  const groupTaskId =
    (liveTask ?? closedTask ?? members.find((m) => m.task_id))?.task_id ?? null;

  return (
    <Card ref={cardRef} className={cn(cardBorderClass)}>
      <CardContent
        className={cn(groupTaskId && "cursor-pointer")}
        onClick={(e) => {
          if (!groupTaskId) return;
          if ((e.target as HTMLElement).closest("button,a,summary")) return;
          onOpenTask(groupTaskId);
        }}
      >
        <div className="flex items-center gap-2">
          {action ? (
            <ActionButton {...action} disabled={busy} />
          ) : (
            <div className="h-8 w-8 shrink-0" />
          )}

          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <div className="min-w-0 flex-1 truncate font-medium leading-snug">
                {group.title}
              </div>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
              {activeRun ? (
                <InputStatusBadge input={activeRun} />
              ) : taskBadge ? (
                <Badge variant={taskBadge}>{taskBadge}</Badge>
              ) : (
                <InputStatusBadge input={newest} />
              )}
              <span className="truncate font-medium">{sendersLabel}</span>
              <MetaDot />
              <span className="font-medium">{fmtWhen(newest.received_at)}</span>
              <MetaDot />
              <span className="font-medium">{members.length} messages</span>
            </div>
          </div>

          <button
            type="button"
            aria-label={open ? "Collapse thread" : "Expand thread"}
            title={open ? "Collapse thread" : "Expand thread"}
            onClick={() => setOpen((v) => !v)}
            className="shrink-0 rounded-md p-1 text-muted-foreground transition-colors hover:text-foreground"
          >
            <Chevron className="h-4 w-4" />
          </button>
        </div>

        <Collapsible open={open}>
          <div
            className="mt-3 space-y-1 border-t pt-2"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Oldest-first reads like a conversation. */}
            {[...members].reverse().map((m) => (
              <GroupMember key={m.id} data={m} />
            ))}
          </div>
        </Collapsible>
      </CardContent>
    </Card>
  );
}

function GroupMember({ data }: { data: RawInput }) {
  const [open, setOpen] = useState(false);
  const MemberChevron = open ? ChevronDown : ChevronRight;
  const header = (
    <>
      <span className="min-w-0 flex-1 truncate text-sm">
        {senderName(data)}
      </span>
      <InputStatusBadge input={data} />
      <span className="shrink-0 text-xs text-muted-foreground">
        {fmtWhen(data.received_at)}
      </span>
    </>
  );

  if (!hasInputDetails(data)) {
    return (
      <div className="rounded-md border bg-muted/30">
        <div className="flex w-full items-center gap-2 px-2 py-1.5 text-left">
          {header}
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-md border bg-muted/30">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
      >
        {header}
        <MemberChevron className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      </button>
      <Collapsible open={open}>
        <div className="space-y-3 border-t px-2 pb-2 pt-2 text-sm">
          <InputBody data={data} />
        </div>
      </Collapsible>
    </div>
  );
}
