import { useEffect, useState } from "react";
import { useOnForeground } from "@/hooks/useOnForeground";
import { api } from "@/lib/api";
import { toast } from "@/lib/toast";
import type { GroceryState } from "@/lib/types";

type Send = (
  call: () => Promise<GroceryState>,
  optimistic?: (state: GroceryState) => GroceryState,
) => Promise<boolean>;

// Cross-offs while shopping fire in quick succession, and every response is
// the whole state as of its own request. Only the newest request's answer is
// shown; when an older one lands late, the optimistic view holds until all
// settle and one fresh read replaces it.
export function useGrocery(): { state: GroceryState | null; send: Send; load: () => void } {
  const [state, setState] = useState<GroceryState | null>(null);
  const [client] = useState(() => {
    let issued = 0;
    let pending = 0;
    let stale = false;
    const send: Send = async (call, optimistic) => {
      if (optimistic) setState((current) => (current ? optimistic(current) : current));
      const seq = ++issued;
      pending += 1;
      let ok = true;
      try {
        const next = await call();
        if (seq === issued) setState(next);
        else stale = true;
      } catch (e) {
        toast.error((e as Error).message);
        ok = false;
        // Take back the optimistic edit.
        if (optimistic) stale = true;
      } finally {
        pending -= 1;
        if (pending === 0 && stale) {
          stale = false;
          void send(api.grocery);
        }
      }
      return ok;
    };
    return { send, load: () => void send(api.grocery) };
  });

  useEffect(() => {
    client.load();
  }, [client]);
  useOnForeground(client.load);

  return { state, send: client.send, load: client.load };
}
