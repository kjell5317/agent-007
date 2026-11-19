import { useState } from "react";
import { Loader2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";

// Remove and Save side by side, equally wide.
export function ModalActions({
  canSave,
  onSave,
  onDelete,
  onDone,
}: {
  canSave: boolean;
  onSave: () => Promise<boolean>;
  onDelete: () => Promise<boolean>;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState<"save" | "delete" | null>(null);

  const run = async (which: "save" | "delete", action: () => Promise<boolean>) => {
    setBusy(which);
    const ok = await action();
    setBusy(null);
    if (ok) onDone();
  };

  return (
    <div className="grid grid-cols-2 gap-2 pt-1">
      <Button
        type="button"
        variant="outline"
        onClick={() => void run("delete", onDelete)}
        disabled={busy !== null}
        className="text-destructive hover:text-destructive"
      >
        {busy === "delete" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
        Remove
      </Button>
      <Button
        type="submit"
        onClick={() => void run("save", onSave)}
        disabled={busy !== null || !canSave}
      >
        {busy === "save" && <Loader2 className="h-4 w-4 animate-spin" />}
        Save
      </Button>
    </div>
  );
}
