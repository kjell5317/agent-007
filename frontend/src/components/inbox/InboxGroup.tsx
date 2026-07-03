import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, CirclePlus, RotateCcw, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { ActionButton, InputBody, MetaDot } from "@/components/inbox/InboxCard";
import { useInboxActions } from "@/components/inbox/useInboxActions";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import { inboxBadge, isAgentTaskFollowup, senderName, type InboxGroup as GroupData } from "@/lib/inbox";
import type { RawInput } from "@/lib/types";

interface Props {
  group: GroupData;
  onChanged: () => Promise<void> | void;
  unseenMemberIds: string[];
  onVisible: (ids: string[]) => void;
}

export function InboxGroup({ group, onChanged, unseenMemberIds, onVisible }: Props) {
  const [open, setOpen] = useState(false);
  const cardRef = useRef<HTMLDivElement>(null);
  const { busy, runTaskAction, promote } = useInboxActions(onChanged);

  const { members, newest, liveTask, closedTask } = group;
  const unseenMemberKey = useMemo(
    () => unseenMemberIds.join("\u0000"),
    [unseenMemberIds],
  );
  // Header shows the task's *status* (a no_change / duplicate follow-up on a
  // closed task is still a closed task); each member below shows its own
  // outcome badge. Groups with no task fall back to the newest member's badge.
  const badge = liveTask ? "open" : closedTask ? "closed" : inboxBadge(newest);

  const senders = Array.from(new Set(members.map(senderName)));
  const sendersLabel =
    senders.length === 1
      ? senders[0]
      : `${senders[0]} +${senders.length - 1}`;

  const unread = unseenMemberIds.length > 0;

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
  const action =
    !liveTask && !closedTask && !agentActed
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
              runTaskAction(liveTask.task_id!, api.markNotTask, "Task dismissed"),
          }
        : closedTask
          ? {
              label: "Re-open task",
              Icon: RotateCcw,
              run: () =>
                runTaskAction(closedTask.task_id!, api.reopenTask, "Task re-opened"),
            }
          : null;

  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <Card ref={cardRef}>
      <CardContent
        className="cursor-pointer"
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("button,a,summary")) return;
          setOpen((v) => !v);
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
              {unread && (
                <span
                  aria-label="Unread"
                  title="Unread"
                  className="inline-block h-2 w-2 shrink-0 rounded-full bg-emerald-500"
                />
              )}
              <div className="min-w-0 flex-1 truncate font-medium leading-snug">
                {group.title}
              </div>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
              <Badge variant={badge}>{badge}</Badge>
              <span className="truncate font-medium">{sendersLabel}</span>
              <MetaDot />
              <span className="font-medium">{fmtWhen(newest.received_at)}</span>
              <MetaDot />
              <span className="font-medium">{members.length} messages</span>
            </div>
          </div>

          <Chevron className="h-4 w-4 shrink-0 text-muted-foreground" />
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
  // Per-member badge is the input's own outcome (duplicate / no_change /
  // updated / reopened / closed) — its individual decision, distinct from the
  // task status shown in the group header.
  const badge = inboxBadge(data);
  return (
    <div className="rounded-md border bg-muted/30">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
      >
        <span className="min-w-0 flex-1 truncate text-sm">
          {senderName(data)}
        </span>
        <Badge variant={badge}>{badge}</Badge>
        <span className="shrink-0 text-xs text-muted-foreground">
          {fmtWhen(data.received_at)}
        </span>
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
