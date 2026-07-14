import { useState } from "react";
import { Check, Pencil, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import { cn } from "@/lib/utils";
import type { Note } from "@/lib/types";

interface Props {
  note: Note;
  onSaved: (note: Note) => void;
  onDeleted: (id: string) => void;
}

type Mode = "view" | "edit" | "confirmDelete";

export function NoteCard({ note, onSaved, onDeleted }: Props) {
  const [mode, setMode] = useState<Mode>("view");
  const [draft, setDraft] = useState(note.content);
  const [busy, setBusy] = useState(false);

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
    if (content === note.content) {
      setMode("view");
      return;
    }
    setBusy(true);
    try {
      const updated = await api.updateNote(note.id, content);
      onSaved(updated);
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
        <div className="flex items-start gap-2">
          <IconButton
            label="Delete note"
            Icon={Trash2}
            disabled={busy}
            onClick={() => setMode("confirmDelete")}
            className="hover:text-destructive"
          />

          <div className="min-w-0 flex-1">
            <div className="whitespace-pre-wrap break-words text-sm leading-snug">
              {note.content}
            </div>
            <NoteMeta note={note} />
          </div>

          <IconButton
            label="Edit note"
            Icon={Pencil}
            disabled={busy}
            onClick={startEdit}
          />
        </div>

        {mode === "confirmDelete" && (
          <div className="mt-3 flex items-center justify-between gap-2 border-t pt-3 text-sm">
            <span className="text-muted-foreground">Delete this note?</span>
            <div className="flex gap-2">
              <Button
                variant="ghost"
                size="sm"
                disabled={busy}
                onClick={() => setMode("view")}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                size="sm"
                disabled={busy}
                onClick={remove}
              >
                <Trash2 className="h-4 w-4" />
                Delete
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function NoteMeta({ note }: { note: Note }) {
  const origin = noteOrigin(note);
  const when = fmtWhen(note.created_at);
  return (
    <div className="mt-1 flex min-w-0 items-center gap-2 overflow-hidden text-xs text-muted-foreground">
      {origin && <span className="min-w-0 truncate font-medium">{origin}</span>}
      {origin && when && (
        <span aria-hidden className="shrink-0">
          •
        </span>
      )}
      {when && <span className="shrink-0 font-medium">{when}</span>}
    </div>
  );
}

// "gmail · alice@example.com", "slack", or "chat" for a note the assistant
// wrote directly (no source input).
function noteOrigin(note: Note): string {
  const source = note.source ?? (note.source_raw_input_id ? null : "chat");
  const parts = [source, note.source_from].filter(Boolean) as string[];
  return parts.join(" · ");
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
