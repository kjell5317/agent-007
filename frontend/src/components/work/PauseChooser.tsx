import { Check, Coffee, Hourglass, Zap, type LucideIcon } from "lucide-react";
import { Modal } from "@/components/ui/modal";
import { PAUSE_REASON_LABEL } from "@/lib/work";
import type { PauseReason } from "@/lib/types";

export const PAUSE_REASON_ICON: Record<PauseReason, LucideIcon> = {
  break: Coffee,
  waiting: Hourglass,
  interrupted: Zap,
};

const REASONS: Array<{ reason: PauseReason; hint: string }> = [
  { reason: "break", hint: "Back in a few minutes" },
  { reason: "waiting", hint: "Off the calendar until you resume" },
  { reason: "interrupted", hint: "Planned again for later" },
];

const OPTION_CLASS =
  "flex w-full items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors hover:bg-accent";

export function PauseChooser({
  onPick,
  onComplete,
  onClose,
}: {
  onPick: (reason: PauseReason) => void;
  onComplete?: () => void;
  onClose: () => void;
}) {
  return (
    <Modal open onClose={onClose} title="Pause">
      <div className="grid gap-2">
        {REASONS.map(({ reason, hint }) => {
          const Icon = PAUSE_REASON_ICON[reason];
          return (
            <button key={reason} type="button" onClick={() => onPick(reason)} className={OPTION_CLASS}>
              <Icon className="h-5 w-5 shrink-0 text-muted-foreground" />
              <span className="min-w-0">
                <span className="block text-sm font-medium">{PAUSE_REASON_LABEL[reason]}</span>
                <span className="block text-xs text-muted-foreground">{hint}</span>
              </span>
            </button>
          );
        })}
        {onComplete && (
          <button type="button" onClick={onComplete} className={OPTION_CLASS}>
            <Check className="h-5 w-5 shrink-0 text-primary" />
            <span className="min-w-0">
              <span className="block text-sm font-medium">Complete</span>
              <span className="block text-xs text-muted-foreground">Done with it</span>
            </span>
          </button>
        )}
      </div>
    </Modal>
  );
}
