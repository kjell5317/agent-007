import { useEffect, useRef, useState } from "react";
import { SearchResultRow } from "@/components/search/SearchResultRow";
import { Modal } from "@/components/ui/modal";
import { api } from "@/lib/api";
import type { SearchHit, Task } from "@/lib/types";

export function PopularSearchResults({ tasks, onOpenTask }: {
  tasks: Task[];
  onOpenTask: (id: string) => void;
}) {
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [taskDetails, setTaskDetails] = useState<ReadonlyMap<string, Task>>(new Map());
  const [preview, setPreview] = useState<SearchHit | null>(null);
  const tasksRef = useRef(tasks);
  tasksRef.current = tasks;

  useEffect(() => {
    let cancelled = false;
    void api.popularSearchResults(25).then(async ({ hits: popular }) => {
      const taskMap = new Map(tasksRef.current.map((task) => [task.id, task]));
      const missing = popular.filter((hit) => hit.type === "task" && !taskMap.has(hit.id));
      const fetched = await Promise.allSettled(missing.map((hit) => api.getTask(hit.id)));
      if (cancelled) return;
      fetched.forEach((result) => {
        if (result.status === "fulfilled") taskMap.set(result.value.id, result.value);
      });
      const shownTaskIds = new Set(popular.filter((hit) => hit.type === "task" && taskMap.has(hit.id)).map((hit) => hit.id));
      setTaskDetails(taskMap);
      setHits(popular.filter((hit) => {
        if (hit.type === "task") return taskMap.has(hit.id);
        return !hit.task_id || !shownTaskIds.has(hit.task_id);
      }).slice(0, 10));
    }).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  if (hits.length === 0) return null;

  return (
    <div className="space-y-2">
      <p className="px-1 text-xs font-medium text-muted-foreground">Most opened</p>
      {hits.map((hit) => (
        <SearchResultRow
          key={`${hit.type}:${hit.id}`}
          hit={hit}
          task={hit.type === "task" ? taskDetails.get(hit.id) : undefined}
          onOpenTask={onOpenTask}
          onActivate={() => { void api.recordSearchClick(hit).catch(() => {}); }}
          onShowContent={() => setPreview(hit)}
        />
      ))}
      <Modal open={preview !== null} onClose={() => setPreview(null)} title={preview?.title || "Result"}>
        <p className="max-h-[60dvh] overflow-y-auto whitespace-pre-wrap text-sm">{preview?.snippet || "No preview available."}</p>
      </Modal>
    </div>
  );
}
