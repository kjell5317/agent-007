import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Note } from "@/lib/types";

export interface NotesData {
  notes: Note[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  // Optimistic local mutations so a save/delete reflects immediately without a
  // full refetch (the API call has already committed by the time these run).
  replaceNote: (note: Note) => void;
  removeNote: (id: string) => void;
}

export function useNotes(): NotesData {
  const [notes, setNotes] = useState<Note[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setNotes(await api.listNotes());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const replaceNote = useCallback((note: Note) => {
    setNotes((prev) => prev.map((n) => (n.id === note.id ? note : n)));
  }, []);

  const removeNote = useCallback((id: string) => {
    setNotes((prev) => prev.filter((n) => n.id !== id));
  }, []);

  return { notes, loading, error, refresh, replaceNote, removeNote };
}
