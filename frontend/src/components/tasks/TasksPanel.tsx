import { useMemo, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { TaskCard } from "@/components/tasks/TaskCard";
import { Collapsible } from "@/components/ui/collapsible";
import { isOverdue, isToday, isTomorrow } from "@/lib/dates";
import type { KotxTask } from "@/lib/kotx";
import { compareTasks, taskGroupDate } from "@/lib/tasks";
import type { Task } from "@/lib/types";
import { runningSince } from "@/lib/work";

interface Props {
  tasks: Task[];
  timeZone: string;
  kotxTasks: ReadonlyMap<number, KotxTask>;
  onChanged: () => Promise<void> | void;
  onKotxChanged: () => Promise<void> | void;
  onTaskOpen: (id: string) => void;
  unseenTaskIds: ReadonlySet<string>;
  onTaskVisible: (id: string) => void;
}

export function TasksPanel({
  tasks,
  timeZone,
  kotxTasks,
  onChanged,
  onKotxChanged,
  onTaskOpen,
  unseenTaskIds,
  onTaskVisible,
}: Props) {
  const [open, setOpen] = useState<Record<CollapsibleKey, boolean>>({ later: false, waiting: false });
  const kotxFor = (task: Task) =>
    task.kotx_task_id != null ? kotxTasks.get(task.kotx_task_id) ?? null : null;
  const [today, tomorrow, later, waiting] = useMemo(() => {
    const sorted = tasks
      .filter((task) => !task.is_container)
      .sort((a, b) => compareTasks(a, b, "scheduled"));
    // The running task leads Today.
    const r: Task[] = [];
    const t: Task[] = [];
    const tm: Task[] = [];
    const l: Task[] = [];
    const w: Task[] = [];
    for (const task of sorted) {
      if (runningSince(task) != null) {
        r.push(task);
        continue;
      }
      // Off the calendar until resumed, so no day to group it under.
      if (task.schedule_state === "waiting") {
        w.push(task);
        continue;
      }
      const groupDate = taskGroupDate(task, "scheduled");
      if (groupDate && (isToday(groupDate) || isOverdue(groupDate))) {
        t.push(task);
      } else if (isTomorrow(groupDate)) {
        tm.push(task);
      } else {
        l.push(task);
      }
    }
    return [[...r, ...t], tm, l, w];
  }, [tasks, timeZone]);

  const groups = [
    { key: "today", title: "Today", tasks: today },
    { key: "tomorrow", title: "Tomorrow", tasks: tomorrow },
    { key: "later", title: "Later", tasks: later },
    { key: "waiting", title: "Waiting", tasks: waiting },
  ].filter((group) => group.tasks.length > 0);

  const renderTask = (task: Task) => (
    <TaskCard
      key={task.id}
      task={task}
      kotxTask={kotxFor(task)}
      onChanged={onChanged}
      onKotxChanged={onKotxChanged}
      onOpen={onTaskOpen}
      unseen={unseenTaskIds.has(task.id)}
      onVisible={onTaskVisible}
    />
  );

  return (
    <div className="space-y-6">
      {groups.length === 0 && (
        <section>
          <SectionHeader title="Today" />
          <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
            No tasks yet. Add one below or sync a source.
          </div>
        </section>
      )}
      {groups.map((group, index) => {
        // Later and Waiting fold away only when a section sits above them.
        if (isCollapsible(group.key) && index > 0) {
          const key = group.key;
          return (
            <CollapsibleSection
              key={key}
              title={group.title}
              open={open[key]}
              onOpenChange={(next) => setOpen((prev) => ({ ...prev, [key]: next }))}
            >
              {group.tasks.map(renderTask)}
            </CollapsibleSection>
          );
        }
        return (
          <section key={group.key}>
            <SectionHeader title={group.title} />
            <div className="space-y-2">{group.tasks.map(renderTask)}</div>
          </section>
        );
      })}
    </div>
  );
}

type CollapsibleKey = "later" | "waiting";

function isCollapsible(key: string): key is CollapsibleKey {
  return key === "later" || key === "waiting";
}

function SectionHeader({ title }: { title: string }) {
  return (
    <h2 className="mb-2 px-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
      {title}
    </h2>
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
