import { useState } from "react";
import { Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DayPicker } from "@/components/ui/date-picker";
import { Modal } from "@/components/ui/modal";
import { fmtAmount } from "@/lib/grocery";
import type { StorageItem } from "@/lib/types";

interface Props {
  item: StorageItem;
  onClose: () => void;
  onSave: (expiresOn: string) => Promise<boolean>;
  onRemove: () => Promise<boolean>;
}

export function StorageItemModal({ item, onClose, onSave, onRemove }: Props) {
  const [expiresOn, setExpiresOn] = useState(item.expires_on);
  const [busy, setBusy] = useState(false);

  const run = async (action: () => Promise<boolean>) => {
    setBusy(true);
    const ok = await action();
    setBusy(false);
    if (ok) onClose();
  };

  return (
    <Modal open onClose={onClose} title={item.name}>
      <div className="space-y-4">
        <p className="text-center text-xs text-muted-foreground">
          {fmtAmount(item.quantity, item.unit)} left · good until
        </p>
        <DayPicker value={expiresOn} onChange={setExpiresOn} />
        <div className="flex gap-2">
          <Button
            type="button"
            variant="ghost"
            onClick={() => void run(onRemove)}
            disabled={busy}
            className="text-destructive hover:text-destructive"
          >
            <Trash2 className="h-4 w-4" />
            Remove
          </Button>
          <Button
            type="button"
            className="flex-1"
            onClick={() => void run(() => onSave(expiresOn))}
            disabled={busy || expiresOn === item.expires_on}
          >
            Save
          </Button>
        </div>
      </div>
    </Modal>
  );
}
