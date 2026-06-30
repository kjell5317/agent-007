import { useState } from "react";
import { ChevronDown, ChevronRight, CirclePlus, RotateCcw, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { ActionButton, InputBody, MetaDot } from "@/components/inbox/InboxCard";
import { useInboxActions } from "@/components/inbox/useInboxActions";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import { inboxBadge, senderName, type InboxGroup as GroupData } from "@/lib/inbox";
import type { RawInput } from "@/lib/types";

interface Props {
  group: GroupData;
  onChanged: () => Promise<void> | void;
  seenAfter: string | null;
}

export function InboxGroup({ group, onChanged, seenAfter }: Props) {
  const [open, setOpen] = useState(false);
  const { busy, runTaskAction, promote } = useInboxActions(onChanged);

  const { members, newest, liveTask, closedTask } = group;
  const rep = liveTask ?? closedTask ?? newest;
  const badge = inboxBadge(rep);

  const senders = Array.from(new Set(members.map(senderName)));
  const sendersLabel =
    senders.length === 1
      ? senders[0]
      : `${senders[0]} +${senders.length - 1}`;

  const unread =
    seenAfter !== null &&
    members.some(
      (m) =>
        m.source !== "manual" &&
        new Date(m.received_at).getTime() > new Date(seenAfter).getTime(),
    );

  // Group-level action mirrors a single card, but "Make a task" feeds the
  // whole thread's context into extraction (anchored on the newest message).
  const action =
    !liveTask && !closedTask
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
    <Card>
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
              <Chevron className="h-4 w-4 shrink-0 text-muted-foreground" />
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
  return (
    <div className="rounded-md border bg-muted/30">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
      >
        <MemberChevron className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate text-sm">
          {senderName(data)}
        </span>
        <span className="shrink-0 text-xs text-muted-foreground">
          {fmtWhen(data.received_at)}
        </span>
      </button>
      <Collapsible open={open}>
        <div className="space-y-3 border-t px-2 pb-2 pt-2 text-sm">
          <InputBody data={data} />
        </div>
      </Collapsible>
    </div>
  );
}
