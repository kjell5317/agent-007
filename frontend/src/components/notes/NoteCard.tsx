import { useState } from "react";
import { Check, ChevronDown, Pencil, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import { cn } from "@/lib/utils";
import type { Note, NoteAudit } from "@/lib/types";

interface Props {
  note: Note;
  onSaved: (note: Note) => void;
  onDeleted: (id: string) => void;
}

type Mode = "view" | "edit";

export function NoteCard({ note, onSaved, onDeleted }: Props) {
  const [mode, setMode] = useState<Mode>("view");
  const [draft, setDraft] = useState(note.content);
  const [busy, setBusy] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [history, setHistory] = useState<NoteAudit[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);

  const toggleHistory = async () => {
    if (showHistory) {
      setShowHistory(false);
      return;
    }
    setShowHistory(true);
    setHistoryError(null);
    try {
      setHistory(await api.noteHistory(note.id));
    } catch (e) {
      setHistoryError((e as Error).message);
    }
  };

  const startEdit = () => {
    setDraft(note.content);
    setMode("edit");
  };

  const save = async () => {
    const content = draft.trim();
    if (!content) {
      toast.error("Note can't be empty");
      return;
    }
    if (content === note.content && !note.needs_review) {
      setMode("view");
      return;
    }
    setBusy(true);
    try {
      const updated = await api.updateNote(note.id, content);
      onSaved(updated);
      setShowHistory(false);
      setHistory(null);
      toast.success("Note updated");
      setMode("view");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      await api.deleteNote(note.id);
      toast.success("Note deleted");
      onDeleted(note.id);
    } catch (e) {
      toast.error((e as Error).message);
      setBusy(false);
      setMode("view");
    }
  };

  if (mode === "edit") {
    return (
      <Card>
        <CardContent className="space-y-2">
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={3}
            autoFocus
            disabled={busy}
            className="text-sm"
          />
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => setMode("view")}
            >
              Cancel
            </Button>
            <Button size="sm" disabled={busy} onClick={save}>
              <Check className="h-4 w-4" />
              Save
            </Button>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardContent>
        <div className="flex items-center gap-2">
          <IconButton
            label="Delete note"
            Icon={Trash2}
            disabled={busy}
            onClick={remove}
            className="hover:text-destructive"
          />

          <div className="min-w-0 flex-1">
            {note.needs_review && (
              <div className="mb-1 text-xs font-medium text-amber-600">
                Needs review · Edit and save to approve
              </div>
            )}
            <div className="whitespace-pre-wrap break-words text-sm leading-snug">
              {note.content}
            </div>
            <NoteMeta note={note} />
            <button
              type="button"
              onClick={() => void toggleHistory()}
              aria-expanded={showHistory}
              className="mt-1 inline-flex items-center gap-1 text-xs text-primary hover:underline"
            >
              History <ChevronDown className={cn("h-3 w-3", showHistory && "rotate-180")} />
            </button>
            {showHistory && (
              <div className="mt-2 space-y-2 border-l pl-3 text-xs">
                {historyError && <p className="text-destructive">{historyError}</p>}
                {!history && !historyError && <p className="text-muted-foreground">Loading history…</p>}
                {history?.length === 0 && (
                  <p className="text-muted-foreground">No changes recorded since history tracking began.</p>
                )}
                {history?.length === 100 && (
                  <p className="text-muted-foreground">Showing the 100 most recent changes.</p>
                )}
                {history?.map((entry, i) => (
                  <div key={i} className="space-y-1">
                    <div className="font-medium">
                      {auditLabel(entry)} · {fmtWhen(entry.occurred_at)}
                      {entry.actor === "manual" ? " · You" : " · Automated"}
                    </div>
                    {entry.action === "content_updated" && (
                      <div className="space-y-1 text-muted-foreground">
                        <div className="whitespace-pre-wrap break-words">Before: {entry.old_content}</div>
                        <div className="whitespace-pre-wrap break-words">After: {entry.new_content}</div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          <IconButton
            label="Edit note"
            Icon={Pencil}
            disabled={busy}
            onClick={startEdit}
          />
        </div>
      </CardContent>
    </Card>
  );
}

function NoteMeta({ note }: { note: Note }) {
  const origin = noteOrigin(note);
  const when = fmtWhen(note.updated_at ?? note.created_at);
  return (
    <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
      {origin && (
        <span className="max-w-full truncate font-medium">{origin}</span>
      )}
      <span className="font-medium">
        {note.content_update_count} text updates tracked
        {note.last_content_update_at ? ` · Last ${fmtWhen(note.last_content_update_at)}` : ""}
      </span>
      {when && <span>Activity {when}</span>}
    </div>
  );
}

function auditLabel(entry: NoteAudit): string {
  switch (entry.action) {
    case "created": return "Created";
    case "content_updated": return "Text updated";
    case "source_linked": return "Source linked";
    case "review_changed": return entry.needs_review ? "Flagged for review" : "Review cleared";
    case "deleted": return "Deleted";
    default: return entry.action;
  }
}

// Use the same sender label as the input inbox; fall back to the source.
function noteOrigin(note: Note): string {
  const from = note.source_from?.trim();
  if (from) {
    const match = from.match(/^"?([^"<]*?)"?\s*<([^>]+)>$/);
    const name = match ? match[1].trim() || match[2].trim() : from;
    return name.replace(/\s*\([^)]*\)\s*$/, "").trim() || name;
  }
  if (note.source === "manual") return "Manual";
  if (note.source === "web_research") return "Web";
  return note.source ?? (note.source_raw_input_id ? "" : "Chat");
}

function IconButton({
  label,
  Icon,
  onClick,
  disabled,
  className,
}: {
  label: string;
  Icon: typeof Trash2;
  onClick: () => void;
  disabled: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-primary disabled:pointer-events-none disabled:opacity-50",
        className,
      )}
    >
      <Icon className="h-5 w-5" />
    </button>
  );
}
