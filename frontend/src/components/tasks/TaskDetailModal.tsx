import type { ReactNode } from "react";
import { ExternalLink, MapPin, Timer } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Modal } from "@/components/ui/modal";
import { useLabels } from "@/hooks/useLabels";
import { fmtDue, fmtWhen } from "@/lib/dates";
import { labelChipClass } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { Task } from "@/lib/types";

interface Props {
  task: Task;
  onClose: () => void;
}

export function TaskDetailModal({ task, onClose }: Props) {
  const labels = useLabels();
  const labelMeta = labels.find((l) => l.name === task.label);

  return (
    <Modal open onClose={onClose} title={task.title}>
      <div className="space-y-4">
        {task.description && (
          <p className="whitespace-pre-wrap text-sm text-muted-foreground">
            {task.description}
          </p>
        )}

        <dl className="grid grid-cols-[5.5rem_1fr] gap-x-3 gap-y-2.5 text-sm">
          <Row label="Status">
            <Badge variant={task.status}>{task.status}</Badge>
          </Row>
          <Row label="Due">{task.due_date ? fmtDue(task.due_date) : "—"}</Row>
          <Row label="Estimation">
            {task.estimation != null ? (
              <span className="inline-flex items-center gap-1">
                <Timer className="h-3.5 w-3.5" />
                {task.estimation} min
              </span>
            ) : (
              "—"
            )}
          </Row>
          <Row label="Label">
            {task.label ? (
              <span
                className={cn(
                  "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium",
                  labelChipClass(labelMeta?.color),
                )}
                title={labelMeta?.description ?? task.label}
              >
                {task.label}
              </span>
            ) : (
              "—"
            )}
          </Row>
          <Row label="Location">
            {task.location ? (
              <span className="inline-flex items-center gap-1">
                <MapPin className="h-3.5 w-3.5" />
                {task.location}
              </span>
            ) : (
              "—"
            )}
          </Row>
          <Row label="Source">{task.is_manual ? "Manual" : "Extracted"}</Row>
          <Row label="Created">{fmtWhen(task.created_at)}</Row>
          <Row label="Updated">{fmtWhen(task.updated_at)}</Row>
        </dl>

        {task.link && (
          <a
            href={task.link}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm font-medium text-primary hover:underline"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            Open source
          </a>
        )}
      </div>
    </Modal>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </>
  );
}
