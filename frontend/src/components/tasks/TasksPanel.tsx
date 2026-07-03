import { useEffect, useMemo, useState } from "react";
import { TaskCard } from "@/components/tasks/TaskCard";
import { TaskDetailModal } from "@/components/tasks/TaskDetailModal";
import { api } from "@/lib/api";
import { isOverdue, isToday } from "@/lib/dates";
import type { KotxTask } from "@/lib/kotx";
import { compareTasksBySchedule, taskGroupDate } from "@/lib/tasks";
import type { Task } from "@/lib/types";

interface Props {
  tasks: Task[];
  kotxTasks: ReadonlyMap<number, KotxTask>;
  onChanged: () => Promise<void> | void;
  onKotxChanged: () => Promise<void> | void;
  selectedTaskId: string | null;
  onTaskOpen: (id: string) => void;
  onSelectedTaskClose: () => void;
  unseenTaskIds: ReadonlySet<string>;
  onTaskVisible: (id: string) => void;
}

export function TasksPanel({
  tasks,
  kotxTasks,
  onChanged,
  onKotxChanged,
  selectedTaskId,
  onTaskOpen,
  onSelectedTaskClose,
  unseenTaskIds,
  onTaskVisible,
}: Props) {
  const [fetchedTask, setFetchedTask] = useState<Task | null>(null);
  const kotxFor = (task: Task) =>
    task.kotx_task_id != null ? kotxTasks.get(task.kotx_task_id) ?? null : null;
  const [today, later] = useMemo(() => {
    const sorted = [...tasks].sort(compareTasksBySchedule);
    const t: Task[] = [];
    const l: Task[] = [];
    for (const task of sorted) {
      const groupDate = taskGroupDate(task);
      if (groupDate && (isToday(groupDate) || isOverdue(groupDate))) {
        t.push(task);
      } else {
        l.push(task);
      }
    }
    return [t, l];
  }, [tasks]);
  const selectedListTask = useMemo(
    () => tasks.find((task) => task.id === selectedTaskId) ?? null,
    [selectedTaskId, tasks],
  );
  const selectedTask = selectedTaskId ? selectedListTask ?? fetchedTask : null;

  useEffect(() => {
    if (!selectedTaskId || selectedListTask) {
      setFetchedTask(null);
      return;
    }
    let cancelled = false;
    api
      .getTask(selectedTaskId)
      .then((task) => {
        if (!cancelled) setFetchedTask(task);
      })
      .catch(() => {
        if (!cancelled) onSelectedTaskClose();
      });
    return () => {
      cancelled = true;
    };
  }, [onSelectedTaskClose, selectedListTask, selectedTaskId]);

  if (today.length === 0 && later.length === 0) {
    return (
      <>
        <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
          No tasks yet. Add one below or sync a source.
        </div>
        {selectedTask && (
          <TaskDetailModal
            task={selectedTask}
            kotxTask={kotxFor(selectedTask)}
            onClose={onSelectedTaskClose}
            onChanged={onChanged}
            onKotxChanged={onKotxChanged}
          />
        )}
      </>
    );
  }

  return (
    <>
      <div className="space-y-6">
        {today.length > 0 && (
          <Section title="Today">
            {today.map((t) => (
              <TaskCard
                key={t.id}
                task={t}
                kotxTask={kotxFor(t)}
                onChanged={onChanged}
                onOpen={onTaskOpen}
                unseen={unseenTaskIds.has(t.id)}
                onVisible={onTaskVisible}
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
                kotxTask={kotxFor(t)}
                onChanged={onChanged}
                onOpen={onTaskOpen}
                unseen={unseenTaskIds.has(t.id)}
                onVisible={onTaskVisible}
              />
            ))}
          </Section>
        )}
      </div>
      {selectedTask && (
        <TaskDetailModal
          task={selectedTask}
          onClose={onSelectedTaskClose}
          onChanged={onChanged}
        />
      )}
    </>
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
