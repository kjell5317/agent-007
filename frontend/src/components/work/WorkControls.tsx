import { Check, Pause, Play, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { WorkActions } from "@/components/work/useWorkActions";
import { cn } from "@/lib/utils";

export function WorkControls({ work, className }: { work: WorkActions; className?: string }) {
  const buttonClass = "h-11 min-w-0 flex-1 text-sm";
  return (
    <div className={cn("flex gap-2", className)}>
      {work.waiting && (
        <Button variant="secondary" className={buttonClass} disabled={work.busy} onClick={work.resume}>
          <RotateCcw className="h-4 w-4" />
          Resume
        </Button>
      )}
      {work.running ? (
        <Button variant="secondary" className={buttonClass} disabled={work.busy} onClick={work.pause}>
          <Pause className="h-4 w-4" />
          Pause
        </Button>
      ) : (
        <Button variant="secondary" className={buttonClass} disabled={work.busy} onClick={work.start}>
          <Play className="h-4 w-4" />
          Start
        </Button>
      )}
      <Button className={buttonClass} disabled={work.busy} onClick={work.complete}>
        <Check className="h-4 w-4" />
        Complete
      </Button>
    </div>
  );
}
