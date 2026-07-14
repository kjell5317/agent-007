import { useMemo, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { TaskCard } from "@/components/tasks/TaskCard";
import { Collapsible } from "@/components/ui/collapsible";
import { useLabels } from "@/hooks/useLabels";
import { isOverdue, isToday, isTomorrow } from "@/lib/dates";
import type { KotxTask } from "@/lib/kotx";
import { labelChipClass, labelChipOutlineClass } from "@/lib/labels";
import { compareTasks, taskGroupDate, type TaskSortMode } from "@/lib/tasks";
import type { Label, Task } from "@/lib/types";
import { cn } from "@/lib/utils";

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
  const [kotxOnly, setKotxOnly] = useState(false);
  const labels = useLabels();
  const kotxFor = (task: Task) =>
    task.kotx_task_id != null ? kotxTasks.get(task.kotx_task_id) ?? null : null;
  const [today, tomorrow, later] = useMemo(() => {
    const sorted = tasks
      .filter((task) => !selectedLabel || task.label === selectedLabel)
      .filter((task) => !kotxOnly || task.kotx_task_id != null)
      .sort((a, b) => compareTasks(a, b, sortMode));
    const t: Task[] = [];
    const tm: Task[] = [];
    const l: Task[] = [];
    for (const task of sorted) {
      const groupDate = taskGroupDate(task, sortMode);
      if (groupDate && (isToday(groupDate) || isOverdue(groupDate))) {
        t.push(task);
      } else if (isTomorrow(groupDate)) {
        tm.push(task);
      } else {
        l.push(task);
      }
    }
    return [t, tm, l];
  }, [selectedLabel, kotxOnly, sortMode, tasks]);

  const groups = [
    { key: "today", title: "Today", tasks: today },
    { key: "tomorrow", title: "Tomorrow", tasks: tomorrow },
    { key: "later", title: "Later", tasks: later },
  ].filter((group) => group.tasks.length > 0);
  // The filter controls live on the first visible section's header, falling
  // back to a standalone "Today" header when everything is filtered away.
  const filterHostKey = groups[0]?.key ?? "today";
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

  const filterActive = Boolean(selectedLabel) || sortMode === "due" || kotxOnly;
  const filters = filtersOpen ? (
    <TaskFilters
      labels={labels}
      sortMode={sortMode}
      onSortModeChange={setSortMode}
      selectedLabel={selectedLabel}
      selectedLabelMeta={selectedLabelMeta}
      onSelectedLabelChange={setSelectedLabel}
      kotxOnly={kotxOnly}
      onKotxOnlyChange={setKotxOnly}
    />
  ) : null;

  return (
    <div className="space-y-6">
      {groups.length === 0 && (
        <section>
          <SectionToggle
            title="Today"
            open={filtersOpen}
            onOpenChange={setFiltersOpen}
            active={filterActive}
          />
          {filters}
          <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
            {emptyMessage}
          </div>
        </section>
      )}
      {groups.map((group) => {
        const isHost = group.key === filterHostKey;
        // Later stays collapsible only when a section sits above it.
        if (group.key === "later" && !isHost) {
          return (
            <CollapsibleSection
              key={group.key}
              title={group.title}
              open={laterOpen}
              onOpenChange={setLaterOpen}
            >
              {group.tasks.map(renderTask)}
            </CollapsibleSection>
          );
        }
        return (
          <section key={group.key}>
            {isHost ? (
              <SectionToggle
                title={group.title}
                open={filtersOpen}
                onOpenChange={setFiltersOpen}
                active={filterActive}
              />
            ) : (
              <SectionHeader title={group.title} />
            )}
            {isHost && filters}
            <div className="space-y-2">{group.tasks.map(renderTask)}</div>
          </section>
        );
      })}
    </div>
  );
}

function SectionHeader({ title }: { title: string }) {
  return (
    <h2 className="mb-2 px-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
      {title}
    </h2>
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
  kotxOnly,
  onKotxOnlyChange,
}: {
  labels: Label[];
  sortMode: TaskSortMode;
  onSortModeChange: (mode: TaskSortMode) => void;
  selectedLabel: string | null;
  selectedLabelMeta: Label | undefined;
  onSelectedLabelChange: (label: string | null) => void;
  kotxOnly: boolean;
  onKotxOnlyChange: (on: boolean) => void;
}) {
  const nextSortMode: TaskSortMode = sortMode === "scheduled" ? "due" : "scheduled";
  // The badge shows the sort currently in effect; clicking it flips to the other.
  const currentSortLabel = sortMode === "scheduled" ? "By Scheduled" : "By Due";
  const dueSortActive = sortMode === "due";

  const toggleLabel = (name: string) => {
    onSelectedLabelChange(selectedLabel === name ? null : name);
  };

  const chipBase =
    "inline-flex h-7 items-center rounded-full border px-3 text-xs font-medium transition-colors";
  const toggleChipClass = (active: boolean) =>
    cn(
      chipBase,
      active
        ? "border-primary bg-primary text-primary-foreground hover:bg-primary/90"
        : "border-input text-muted-foreground hover:bg-accent hover:text-foreground",
    );

  return (
    <div className="mb-3 flex flex-wrap gap-2 px-1">
      <button
        type="button"
        aria-pressed={dueSortActive}
        onClick={() => onSortModeChange(nextSortMode)}
        className={toggleChipClass(dueSortActive)}
      >
        {currentSortLabel}
      </button>
      <button
        type="button"
        aria-pressed={kotxOnly}
        onClick={() => onKotxOnlyChange(!kotxOnly)}
        className={toggleChipClass(kotxOnly)}
      >
        kotx
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
