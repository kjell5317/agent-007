import { NoteCard } from "@/components/notes/NoteCard";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useNotes } from "@/hooks/useNotes";

export function NotesPanel() {
  const { notes, loading, error, refresh, replaceNote, removeNote } = useNotes();

  if (loading && notes.length === 0) {
    return (
      <div className="flex justify-center py-12" role="status" aria-label="Loading notes">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-2">
        <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
          Couldn't load notes: {error}
        </div>
        <div className="flex justify-center">
          <Button variant="outline" size="sm" onClick={() => refresh()}>
            Retry
          </Button>
        </div>
      </div>
    );
  }

  if (notes.length === 0) {
    return (
      <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
        No notes yet. The agent saves notes as it processes your inbox.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {notes.map((note) => (
        <NoteCard
          key={note.id}
          note={note}
          onSaved={replaceNote}
          onDeleted={removeNote}
        />
      ))}
    </div>
  );
}
