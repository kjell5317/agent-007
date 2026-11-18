import { useState } from "react";
import { Check, NotebookPen, Pencil, Trash2 } from "lucide-react";
import { toast } from "@/lib/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import { displaySender, sourceLabel } from "@/lib/inbox";
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
  const [expanded, setExpanded] = useState(false);
  const [history, setHistory] = useState<NoteAudit[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const hasHistory = note.history_count > 0;

  const loadHistory = async () => {
    if (!hasHistory || loadingHistory || history !== null) return;
    setHistoryError(null);
    setLoadingHistory(true);
    try {
      const entries = await api.noteHistory(note.id);
      setHistory(entries);
    } catch (e) {
      setHistoryError((e as Error).message);
    } finally {
      setLoadingHistory(false);
    }
  };

  const toggleExpanded = () => {
    if (!expanded) void loadHistory();
    setExpanded(!expanded);
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
    <Card className={expanded ? "min-h-[76px]" : "h-[76px] overflow-hidden"}>
      <CardContent
        role="button"
        tabIndex={0}
        aria-label={`${expanded ? "Collapse" : "Expand"} note: ${note.content.slice(0, 80)}`}
        aria-expanded={expanded}
        aria-busy={loadingHistory || undefined}
        className="cursor-pointer"
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("button,a,summary")) return;
          toggleExpanded();
        }}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggleExpanded();
          }
        }}
      >
        <div className="flex items-center gap-2">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center text-muted-foreground" aria-hidden="true">
            <NotebookPen className="h-5 w-5" />
          </span>

          <div className="min-w-0 flex-1">
            {note.needs_review && expanded && (
              <div className="mb-1 text-xs font-medium text-amber-600">
                Needs review · Edit and save to approve
              </div>
            )}
            <div className={cn("whitespace-pre-wrap break-words leading-snug", expanded ? "text-sm" : "line-clamp-1 text-base font-medium")}>
              {note.content}
            </div>
            <NoteMeta note={note} />
          </div>

          {expanded && (
            <div className="flex shrink-0 items-center gap-1">
              <IconButton
                label="Delete note"
                Icon={Trash2}
                disabled={busy}
                onClick={remove}
                className="hover:text-destructive"
              />
              <IconButton
                label="Edit note"
                Icon={Pencil}
                disabled={busy}
                onClick={startEdit}
              />
            </div>
          )}
        </div>
        <Collapsible open={expanded && hasHistory}>
          <div className="mt-3 space-y-2 border-t pt-3 text-xs" onClick={(e) => e.stopPropagation()}>
            {historyError && <p className="text-destructive">{historyError}</p>}
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
                  </div>
                )}
              </div>
            ))}
          </div>
        </Collapsible>
      </CardContent>
    </Card>
  );
}

// Same layout as an inbox card: a datetime badge, then the sender as plain text.
function NoteMeta({ note }: { note: Note }) {
  const origin = noteOrigin(note);
  const when = fmtWhen(note.updated_at ?? note.created_at);
  return (
    <div className="mt-1 flex min-w-0 items-center gap-2 overflow-hidden text-xs text-muted-foreground">
      {when && <Badge variant="muted" className="shrink-0">{when}</Badge>}
      {origin && <span className="min-w-0 flex-1 truncate font-medium">{origin}</span>}
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
  if (from) return displaySender(from);
  if (note.source) return sourceLabel(note.source);
  return note.source_raw_input_id ? "" : "Chat";
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
      onClick={(event) => { event.stopPropagation(); onClick(); }}
      className={cn(
        "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-primary disabled:pointer-events-none disabled:opacity-50",
        className,
      )}
    >
      <Icon className="h-5 w-5" />
    </button>
  );
}
