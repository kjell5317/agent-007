import { useMemo, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { TaskCard } from "@/components/tasks/TaskCard";
import { Collapsible } from "@/components/ui/collapsible";
import { useLabels } from "@/hooks/useLabels";
import { isOverdue, isToday } from "@/lib/dates";
import type { KotxTask } from "@/lib/kotx";
import { labelChipClass, labelChipOutlineClass } from "@/lib/labels";
import { compareTasks, taskGroupDate, type TaskSortMode } from "@/lib/tasks";
import type { Label, Task } from "@/lib/types";
import { cn } from "@/lib/utils";

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
  const [sortMode, setSortMode] = useState<TaskSortMode>("scheduled");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [selectedLabel, setSelectedLabel] = useState<string | null>(null);
  const labels = useLabels();
  const kotxFor = (task: Task) =>
    task.kotx_task_id != null ? kotxTasks.get(task.kotx_task_id) ?? null : null;
  const [today, thisWeek, later] = useMemo(() => {
    const sorted = tasks
      .filter((task) => !selectedLabel || task.label === selectedLabel)
      .sort((a, b) => compareTasks(a, b, sortMode));
    const t: Task[] = [];
    const w: Task[] = [];
    const l: Task[] = [];
    for (const task of sorted) {
      const groupDate = taskGroupDate(task, sortMode);
      if (groupDate && (isToday(groupDate) || isOverdue(groupDate))) {
        t.push(task);
      } else if (isMoreThanOneWeekAhead(groupDate)) {
        l.push(task);
      } else {
        w.push(task);
      }
    }
    return [t, w, l];
  }, [selectedLabel, sortMode, tasks]);

  const hasVisibleTasks =
    today.length > 0 || thisWeek.length > 0 || later.length > 0;
  const selectedLabelMeta = labels.find((l) => l.name === selectedLabel);
  const emptyMessage = selectedLabel
    ? `No tasks with label "${selectedLabel}".`
    : "No tasks yet. Add one below or sync a source.";

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
      <section>
        <SectionToggle
          title="Today"
          open={filtersOpen}
          onOpenChange={setFiltersOpen}
          active={Boolean(selectedLabel) || sortMode === "due"}
        />
        {filtersOpen && (
          <TaskFilters
            labels={labels}
            sortMode={sortMode}
            onSortModeChange={setSortMode}
            selectedLabel={selectedLabel}
            selectedLabelMeta={selectedLabelMeta}
            onSelectedLabelChange={setSelectedLabel}
          />
        )}
        <div className="space-y-2">{today.map(renderTask)}</div>
      </section>
      {!hasVisibleTasks && (
        <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
          {emptyMessage}
        </div>
      )}
      {thisWeek.length > 0 && (
        <Section title="This week">{thisWeek.map(renderTask)}</Section>
      )}
      {later.length > 0 && (
        <CollapsibleSection
          title="Later"
          open={laterOpen}
          onOpenChange={setLaterOpen}
        >
          {later.map(renderTask)}
        </CollapsibleSection>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <div className="mb-2 flex items-start gap-2 px-1">
        <h2 className="min-w-0 flex-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {title}
        </h2>
      </div>
      <div className="space-y-2">{children}</div>
    </section>
  );
}

function SectionToggle({
  title,
  open,
  onOpenChange,
  active,
}: {
  title: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  active: boolean;
}) {
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <button
      type="button"
      onClick={() => onOpenChange(!open)}
      aria-expanded={open}
      aria-label="Toggle filters"
      className="mb-2 flex w-full items-center gap-2 px-1 text-left"
    >
      <span className="min-w-0 flex-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {title}
      </span>
      <Chevron
        className={cn(
          "h-3.5 w-3.5 shrink-0",
          active && !open ? "text-primary" : "text-muted-foreground",
        )}
      />
    </button>
  );
}

function TaskFilters({
  labels,
  sortMode,
  onSortModeChange,
  selectedLabel,
  selectedLabelMeta,
  onSelectedLabelChange,
}: {
  labels: Label[];
  sortMode: TaskSortMode;
  onSortModeChange: (mode: TaskSortMode) => void;
  selectedLabel: string | null;
  selectedLabelMeta: Label | undefined;
  onSelectedLabelChange: (label: string | null) => void;
}) {
  const nextSortMode: TaskSortMode = sortMode === "scheduled" ? "due" : "scheduled";
  const nextSortLabel = sortMode === "scheduled" ? "By Due" : "By Scheduled";

  const toggleLabel = (name: string) => {
    onSelectedLabelChange(selectedLabel === name ? null : name);
  };

  const chipBase =
    "inline-flex h-7 items-center rounded-full border px-3 text-xs font-medium transition-colors";

  return (
    <div className="mb-3 flex flex-wrap gap-2 px-1">
      <button
        type="button"
        onClick={() => onSortModeChange(nextSortMode)}
        className={cn(chipBase, "border-input bg-background text-foreground hover:bg-accent")}
      >
        {nextSortLabel}
      </button>
      {labels.map((label) => {
        const selected = selectedLabel === label.name;
        return (
          <button
            key={label.name}
            type="button"
            aria-pressed={selected}
            title={label.description || label.name}
            onClick={() => toggleLabel(label.name)}
            className={cn(
              chipBase,
              selected
                ? cn("border-transparent", labelChipClass(label.color))
                : labelChipOutlineClass(label.color),
            )}
          >
            {label.name}
          </button>
        );
      })}
      {selectedLabel && !selectedLabelMeta && (
        <button
          type="button"
          aria-pressed="true"
          onClick={() => onSelectedLabelChange(null)}
          className={cn(chipBase, "border-transparent", labelChipClass(undefined))}
        >
          {selectedLabel}
        </button>
      )}
    </div>
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
