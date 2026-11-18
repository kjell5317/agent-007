import { Coffee, Hourglass, Zap, type LucideIcon } from "lucide-react";
import { Modal } from "@/components/ui/modal";
import { PAUSE_REASON_LABEL } from "@/lib/work";
import type { PauseReason } from "@/lib/types";

const REASONS: Array<{ reason: PauseReason; icon: LucideIcon; hint: string }> = [
  { reason: "break", icon: Coffee, hint: "Back in a few minutes" },
  { reason: "waiting", icon: Hourglass, hint: "Off the calendar until you resume" },
  { reason: "interrupted", icon: Zap, hint: "Planned again for later" },
];

export function PauseChooser({
  busy,
  onPick,
  onClose,
}: {
  busy: boolean;
  onPick: (reason: PauseReason) => void;
  onClose: () => void;
}) {
  return (
    <Modal open onClose={onClose} title="Pause">
      <div className="grid gap-2">
        {REASONS.map(({ reason, icon: Icon, hint }) => (
          <button
            key={reason}
            type="button"
            disabled={busy}
            onClick={() => onPick(reason)}
            className="flex w-full items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors hover:bg-accent disabled:pointer-events-none disabled:opacity-50"
          >
            <Icon className="h-5 w-5 shrink-0 text-muted-foreground" />
            <span className="min-w-0">
              <span className="block text-sm font-medium">{PAUSE_REASON_LABEL[reason]}</span>
              <span className="block text-xs text-muted-foreground">{hint}</span>
            </span>
          </button>
        ))}
      </div>
    </Modal>
  );
}
