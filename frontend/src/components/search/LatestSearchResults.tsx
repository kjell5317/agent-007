import { useEffect, useMemo, useState } from "react";
import { SearchResultRow } from "@/components/search/SearchResultRow";
import { Modal } from "@/components/ui/modal";
import { api } from "@/lib/api";
import { latestHits } from "@/lib/latest";
import type { Note, RawInput, SearchHit, Task } from "@/lib/types";

const SHOWN = 15;
// Notes come newest-edited first; fetch enough that the newest created are in.
const NOTES_FETCHED = 50;

export function LatestSearchResults({ tasks, inputs, onOpenTask, onChanged }: {
  tasks: Task[];
  inputs: RawInput[];
  onOpenTask: (id: string) => void;
  onChanged: () => Promise<void> | void;
}) {
  const [notes, setNotes] = useState<Note[]>([]);
  const [preview, setPreview] = useState<SearchHit | null>(null);

  useEffect(() => {
    let active = true;
    api.listNotes(NOTES_FETCHED).then((rows) => {
      if (active) setNotes(rows);
    }).catch(() => {});
    return () => { active = false; };
  }, []);

  const hits = useMemo(() => latestHits(tasks, inputs, notes, SHOWN), [tasks, inputs, notes]);
  const taskById = useMemo(() => new Map(tasks.map((task) => [task.id, task])), [tasks]);
  const inputById = useMemo(() => new Map(inputs.map((input) => [input.id, input])), [inputs]);

  if (hits.length === 0) return null;

  return (
    <div className="space-y-2">
      {hits.map((hit) => (
        <SearchResultRow
          key={`${hit.type}:${hit.id}`}
          hit={hit}
          task={hit.type === "task" ? taskById.get(hit.id) : undefined}
          input={hit.type === "input" ? inputById.get(hit.id) : undefined}
          onChanged={onChanged}
          onOpenTask={onOpenTask}
          onShowContent={() => setPreview(hit)}
        />
      ))}
      <Modal open={preview !== null} onClose={() => setPreview(null)} title={preview?.title || "Result"}>
        <p className="max-h-[60dvh] overflow-y-auto whitespace-pre-wrap text-sm">{preview?.snippet || "No preview available."}</p>
      </Modal>
    </div>
  );
}
