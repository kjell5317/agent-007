import { useMemo } from "react";
import { TaskCard } from "@/components/tasks/TaskCard";
import { isOverdue, isToday } from "@/lib/dates";
import type { Task } from "@/lib/types";

interface Props {
  tasks: Task[];
  onChanged: () => Promise<void> | void;
  seenAfter: string | null;
}

function sortTasks(a: Task, b: Task) {
  const ad = a.due_date ? new Date(a.due_date).getTime() : Infinity;
  const bd = b.due_date ? new Date(b.due_date).getTime() : Infinity;
  if (ad !== bd) return ad - bd;
  return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
}

export function TasksPanel({ tasks, onChanged, seenAfter }: Props) {
  const [today, later] = useMemo(() => {
    const sorted = [...tasks].sort(sortTasks);
    const t: Task[] = [];
    const l: Task[] = [];
    for (const task of sorted) {
      if (task.due_date && (isToday(task.due_date) || isOverdue(task.due_date))) {
        t.push(task);
      } else {
        l.push(task);
      }
    }
    return [t, l];
  }, [tasks]);

  if (today.length === 0 && later.length === 0) {
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
              onChanged={onChanged}
              seenAfter={seenAfter}
            />
          ))}
        </Section>
      )}
      {later.length > 0 && (
        <Section title="Later">
          {later.map((t) => (
            <TaskCard
              key={t.id}
              task={t}
              onChanged={onChanged}
              seenAfter={seenAfter}
            />
          ))}
        </Section>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h2 className="mb-2 px-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {title}
      </h2>
      <div className="space-y-2">{children}</div>
    </section>
  );
}
