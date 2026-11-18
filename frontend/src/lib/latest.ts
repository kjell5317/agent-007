import { inputGroupKey, inputTitle, senderName } from "@/lib/inbox";
import type { Note, RawInput, SearchHit, Task } from "@/lib/types";

// Shaped like the search's own hits, so the rows render alike; `ts` is the
// creation time the list sorts by.
function taskHit(task: Task): SearchHit {
  return {
    type: "task", id: task.id, title: task.title, snippet: task.description,
    url: task.link, task_id: task.id, source: null, sender: null,
    status: task.status, ts: task.created_at, score: 0,
  };
}

function inputHit(input: RawInput): SearchHit {
  return {
    type: "input", id: input.id, title: inputTitle(input), snippet: input.content,
    url: null, task_id: input.task_id, source: input.source,
    sender: senderName(input), status: input.status, ts: input.received_at, score: 0,
  };
}

function noteHit(note: Note): SearchHit {
  return {
    type: "note", id: note.id, title: note.content.slice(0, 80), snippet: note.content,
    url: null, task_id: null, source: note.source, sender: note.source_from,
    status: null, ts: note.created_at, score: 0,
  };
}

// The empty search view: the newest tasks, messages and notes, by creation.
export function latestHits(
  tasks: readonly Task[], inputs: readonly RawInput[], notes: readonly Note[], limit: number,
): SearchHit[] {
  // A repeating task's instances are created a week ahead, one per day for a
  // daily one. Its series shows once, as old as its oldest open instance.
  const series = new Map<string, Task>();
  const single: Task[] = [];
  for (const task of tasks) {
    if (!task.recurrence_series_id) {
      single.push(task);
      continue;
    }
    const kept = series.get(task.recurrence_series_id);
    if (!kept || Date.parse(task.created_at) < Date.parse(kept.created_at)) series.set(task.recurrence_series_id, task);
  }
  // A message filed into a task shows as that task; a thread as its newest message.
  const threads = new Map<string, RawInput>();
  for (const input of inputs) {
    if (input.task_id || input.status === "open") continue;
    const kept = threads.get(inputGroupKey(input));
    if (!kept || Date.parse(input.received_at) > Date.parse(kept.received_at)) threads.set(inputGroupKey(input), input);
  }
  return [
    ...[...single, ...series.values()].map(taskHit),
    ...[...threads.values()].map(inputHit),
    ...notes.filter((note) => !note.needs_review).map(noteHit),
  ]
    .sort((a, b) => Date.parse(b.ts!) - Date.parse(a.ts!))
    .slice(0, limit);
}
