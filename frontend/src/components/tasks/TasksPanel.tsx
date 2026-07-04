import { useMemo, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { TaskCard } from "@/components/tasks/TaskCard";
import { Collapsible } from "@/components/ui/collapsible";
import { isOverdue, isToday } from "@/lib/dates";
import type { KotxTask } from "@/lib/kotx";
import { compareTasksBySchedule, taskGroupDate } from "@/lib/tasks";
import type { Task } from "@/lib/types";

const ONE_WEEK_MS = 7 * 24 * 60 * 60 * 1000;

function isMoreThanOneWeekAhead(iso: string | null): boolean {
  if (!iso) return false;
  return new Date(iso).getTime() > Date.now() + ONE_WEEK_MS;
}

interface Props {
  tasks: Task[];
  kotxTasks: ReadonlyMap<number, KotxTask>;
  onChanged: () => Promise<void> | void;
  onKotxChanged: () => Promise<void> | void;
  onTaskOpen: (id: string) => void;
  unseenTaskIds: ReadonlySet<string>;
  onTaskVisible: (id: string) => void;
}

export function TasksPanel({
  tasks,
  kotxTasks,
  onChanged,
  onKotxChanged,
  onTaskOpen,
  unseenTaskIds,
  onTaskVisible,
}: Props) {
  const [laterOpen, setLaterOpen] = useState(false);
  const kotxFor = (task: Task) =>
    task.kotx_task_id != null ? kotxTasks.get(task.kotx_task_id) ?? null : null;
  const [today, thisWeek, later] = useMemo(() => {
    const sorted = [...tasks].sort(compareTasksBySchedule);
    const t: Task[] = [];
    const w: Task[] = [];
    const l: Task[] = [];
    for (const task of sorted) {
      const groupDate = taskGroupDate(task);
      if (groupDate && (isToday(groupDate) || isOverdue(groupDate))) {
        t.push(task);
      } else if (isMoreThanOneWeekAhead(groupDate)) {
        l.push(task);
      } else {
        w.push(task);
      }
    }
    return [t, w, l];
  }, [tasks]);

  if (today.length === 0 && thisWeek.length === 0 && later.length === 0) {
    return (
      <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
        No tasks yet. Add one below or sync a source.
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {today.length > 0 && (
        <Section title="Today">
          {today.map((t) => (
            <TaskCard
              key={t.id}
              task={t}
              kotxTask={kotxFor(t)}
              onChanged={onChanged}
              onKotxChanged={onKotxChanged}
              onOpen={onTaskOpen}
              unseen={unseenTaskIds.has(t.id)}
              onVisible={onTaskVisible}
            />
          ))}
        </Section>
      )}
      {thisWeek.length > 0 && (
        <Section title="This week">
          {thisWeek.map((t) => (
            <TaskCard
              key={t.id}
              task={t}
              kotxTask={kotxFor(t)}
              onChanged={onChanged}
              onKotxChanged={onKotxChanged}
              onOpen={onTaskOpen}
              unseen={unseenTaskIds.has(t.id)}
              onVisible={onTaskVisible}
            />
          ))}
        </Section>
      )}
      {later.length > 0 && (
        <CollapsibleSection
          title="Later"
          open={laterOpen}
          onOpenChange={setLaterOpen}
        >
          {later.map((t) => (
            <TaskCard
              key={t.id}
              task={t}
              kotxTask={kotxFor(t)}
              onChanged={onChanged}
              onKotxChanged={onKotxChanged}
              onOpen={onTaskOpen}
              unseen={unseenTaskIds.has(t.id)}
              onVisible={onTaskVisible}
            />
          ))}
        </CollapsibleSection>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h2 className="mb-2 px-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {title}
      </h2>
      <div className="space-y-2">{children}</div>
    </section>
  );
}

function CollapsibleSection({
  title,
  open,
  onOpenChange,
  children,
}: {
  title: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
}) {
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <section>
      <button
        type="button"
        onClick={() => onOpenChange(!open)}
        aria-expanded={open}
        className="mb-2 flex w-full items-center gap-2 px-1 text-left"
      >
        <span className="min-w-0 flex-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {title}
        </span>
        <Chevron className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      </button>
      <Collapsible open={open}>
        <div className="space-y-2">{children}</div>
      </Collapsible>
    </section>
  );
}
