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

export function useNotes(source = ""): NotesData {
  const [notes, setNotes] = useState<Note[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setNotes(await api.listNotes(500, source));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [source]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 60_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const replaceNote = useCallback((note: Note) => {
    setNotes((prev) => prev.map((n) => (n.id === note.id ? note : n)));
  }, []);

  const removeNote = useCallback((id: string) => {
    setNotes((prev) => prev.filter((n) => n.id !== id));
  }, []);

  return { notes, loading, error, refresh, replaceNote, removeNote };
}
