import { useEffect, useRef, useState } from "react";
import { SearchResultRow } from "@/components/search/SearchResultRow";
import { Modal } from "@/components/ui/modal";
import { api } from "@/lib/api";
import type { RawInput, SearchHit, Task } from "@/lib/types";

export function PopularSearchResults({ tasks, inputs, onOpenTask, onChanged }: {
  tasks: Task[];
  inputs: RawInput[];
  onOpenTask: (id: string) => void;
  onChanged: () => Promise<void> | void;
}) {
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [taskDetails, setTaskDetails] = useState<ReadonlyMap<string, Task>>(new Map());
  const [inputDetails, setInputDetails] = useState<ReadonlyMap<string, RawInput>>(new Map());
  const [preview, setPreview] = useState<SearchHit | null>(null);
  const tasksRef = useRef(tasks);
  tasksRef.current = tasks;
  const inputsRef = useRef(inputs);
  inputsRef.current = inputs;

  useEffect(() => {
    let cancelled = false;
    void api.popularSearchResults(25).then(async ({ hits: popular }) => {
      const taskMap = new Map(tasksRef.current.map((task) => [task.id, task]));
      const missing = popular.filter((hit) => hit.type === "task" && !taskMap.has(hit.id));
      const fetched = await Promise.allSettled(missing.map((hit) => api.getTask(hit.id)));
      const inputMap = new Map(inputsRef.current.map((input) => [input.id, input]));
      const missingInputs = popular.filter((hit) => hit.type === "input" && !inputMap.has(hit.id));
      const fetchedInputs = await Promise.allSettled(missingInputs.map((hit) => api.getInput(hit.id)));
      if (cancelled) return;
      fetched.forEach((result) => {
        if (result.status === "fulfilled") taskMap.set(result.value.id, result.value);
      });
      fetchedInputs.forEach((result) => {
        if (result.status === "fulfilled") inputMap.set(result.value.id, result.value);
      });
      const shownTaskIds = new Set(popular.filter((hit) => hit.type === "task" && taskMap.has(hit.id)).map((hit) => hit.id));
      setTaskDetails(taskMap);
      setInputDetails(inputMap);
      setHits(popular.filter((hit) => {
        if (hit.type === "task") return taskMap.has(hit.id);
        if (hit.type === "input") return inputMap.has(hit.id);
        return !hit.task_id || !shownTaskIds.has(hit.task_id);
      }).slice(0, 5));
    }).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  if (hits.length === 0) return null;

  return (
    <div className="space-y-2">
      {hits.map((hit) => (
        <SearchResultRow
          key={`${hit.type}:${hit.id}`}
          hit={hit}
          task={hit.type === "task" ? taskDetails.get(hit.id) : undefined}
          input={hit.type === "input" ? inputDetails.get(hit.id) : undefined}
          onChanged={onChanged}
          onOpenTask={onOpenTask}
          onActivate={() => { if (hit.type !== "task") void api.recordSearchClick(hit).catch(() => {}); }}
          onShowContent={() => setPreview(hit)}
        />
      ))}
      <Modal open={preview !== null} onClose={() => setPreview(null)} title={preview?.title || "Result"}>
        <p className="max-h-[60dvh] overflow-y-auto whitespace-pre-wrap text-sm">{preview?.snippet || "No preview available."}</p>
      </Modal>
    </div>
  );
}
