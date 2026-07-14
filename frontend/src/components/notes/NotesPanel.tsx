import { NoteCard } from "@/components/notes/NoteCard";
import { Button } from "@/components/ui/button";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { useNotes } from "@/hooks/useNotes";

export function NotesPanel() {
  const { notes, loading, error, refresh, replaceNote, removeNote } = useNotes();

  if (loading && notes.length === 0) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 3 }).map((_, i) => (
          <SkeletonBlock key={i} className="h-20 w-full rounded-xl" />
        ))}
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
