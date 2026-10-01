import { useState } from "react";
import { Check, ChevronDown, Pencil, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible } from "@/components/ui/collapsible";
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
  const [expanded, setExpanded] = useState(false);
  const [history, setHistory] = useState<NoteAudit[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const hasHistory = note.history_count > 0;

  const toggleHistory = async () => {
    if (!hasHistory || loadingHistory) return;
    if (showHistory) {
      setShowHistory(false);
      return;
    }
    setHistoryError(null);
    setLoadingHistory(true);
    try {
      const entries = await api.noteHistory(note.id);
      setHistory(entries);
      setShowHistory(entries.length > 0);
    } catch (e) {
      setHistoryError((e as Error).message);
      setShowHistory(true);
    } finally {
      setLoadingHistory(false);
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
          setExpanded((current) => !current);
        }}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded((current) => !current);
          }
        }}
      >
        <div className="flex items-center gap-2">
          <IconButton
            label="Delete note"
            Icon={Trash2}
            disabled={busy}
            onClick={remove}
            className="hover:text-destructive"
          />

          <div className="min-w-0 flex-1">
            {note.needs_review && expanded && (
              <div className="mb-1 text-xs font-medium text-amber-600">
                Needs review · Edit and save to approve
              </div>
            )}
            <div className={cn("whitespace-pre-wrap break-words text-sm leading-snug", !expanded && "line-clamp-1")}>
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
          <ChevronDown className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", expanded && "rotate-180")} aria-hidden="true" />
        </div>
        {expanded && hasHistory && (
          <Button type="button" variant="ghost" size="sm" className="ml-8 mt-2" onClick={(e) => { e.stopPropagation(); void toggleHistory(); }}>
            {showHistory ? "Hide history" : "History"}
          </Button>
        )}
        <Collapsible open={expanded && showHistory}>
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
                    <div className="whitespace-pre-wrap break-words">After: {entry.new_content}</div>
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

function NoteMeta({ note }: { note: Note }) {
  const origin = noteOrigin(note);
  const when = fmtWhen(note.updated_at ?? note.created_at);
  return (
    <div className="mt-1 flex min-w-0 items-center gap-2 overflow-hidden text-xs text-muted-foreground">
      {origin && (
        <span className="min-w-0 flex-1 truncate font-medium">{origin}</span>
      )}
      {!origin && <span className="flex-1" />}
      {when && <span className="shrink-0 font-medium">{when}</span>}
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
