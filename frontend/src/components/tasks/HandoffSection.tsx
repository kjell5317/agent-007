import { useState } from "react";
import { Bot, ExternalLink, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import {
  handoffActive,
  handoffChip,
  HANDOFF_KIND_LABEL,
  manualHandoffKinds,
  resultList,
  resultText,
} from "@/lib/handoff";
import { cn } from "@/lib/utils";
import type { HandoffKind, Task } from "@/lib/types";

const TONE_CLASS = {
  proposed: "bg-sky-100 text-sky-800 dark:bg-sky-500/20 dark:text-sky-200",
  running: "bg-violet-100 text-violet-800 dark:bg-violet-500/20 dark:text-violet-200",
  ready: "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/20 dark:text-emerald-200",
  failed: "bg-rose-100 text-rose-800 dark:bg-rose-500/20 dark:text-rose-200",
} as const;

export function HandoffChip({ task, className }: { task: Task; className?: string }) {
  const chip = handoffChip(task.handoff);
  if (!chip) return null;
  return (
    <span
      title={task.handoff?.brief}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
        TONE_CLASS[chip.tone],
        className,
      )}
    >
      <Bot className="h-3 w-3" />
      {chip.text}
    </span>
  );
}

export function HandoffSection({
  task,
  onChanged,
}: {
  task: Task;
  onChanged: () => Promise<void> | void;
}) {
  const [pending, setPending] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [brief, setBrief] = useState("");
  const handoff = task.handoff;
  const shown = handoff && handoff.state !== "declined" ? handoff : null;

  async function act(action: () => Promise<unknown>, message: string) {
    setPending(true);
    try {
      await action();
      toast.success(message);
      setMenuOpen(false);
      setBrief("");
      await onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setPending(false);
    }
  }

  const start = (kind: HandoffKind, text = brief) =>
    act(() => api.startHandoff(task.id, { kind, brief: text.trim() || undefined }), "Agent started");

  return (
    <section className="space-y-2">
      {shown && (
        <div className="space-y-2 rounded-md border px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <HandoffChip task={task} />
            <span className="text-xs text-muted-foreground">{HANDOFF_KIND_LABEL[shown.kind]}</span>
          </div>
          <p className="text-sm text-muted-foreground">{shown.brief}</p>

          {shown.state === "proposed" && (
            <div className="flex gap-2">
              <Button size="sm" disabled={pending}
                onClick={() => act(() => api.startHandoff(task.id), "Opening the issue")}>
                Open issue
              </Button>
              <Button size="sm" variant="outline" disabled={pending}
                onClick={() => act(() => api.declineHandoff(task.id), "Declined")}>
                Decline
              </Button>
            </div>
          )}

          {shown.state === "running" && (
            <p className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
              <RefreshCw className="h-3 w-3 animate-spin" /> Working on it
            </p>
          )}

          {shown.state === "ready" && shown.kind === "code_change" && resultText(shown.result, "issue_url") && (
            <a className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline"
              href={resultText(shown.result, "issue_url")!} target="_blank" rel="noreferrer">
              Open the issue <ExternalLink className="h-3 w-3" />
            </a>
          )}

          {shown.state === "ready" && (shown.kind === "web_research" || shown.kind === "own_data_lookup") && (
            <p className="text-xs text-muted-foreground">Added to the description.</p>
          )}

          {shown.state === "ready" && shown.kind === "email_reply" && (
            <div className="space-y-2">
              {resultList(shown.result, "open_questions").length > 0 && (
                <div className="text-sm">
                  <span className="text-xs font-medium uppercase text-muted-foreground">Fill in before sending</span>
                  <ul className="ml-4 list-disc">
                    {resultList(shown.result, "open_questions").map((question) => <li key={question}>{question}</li>)}
                  </ul>
                </div>
              )}
              <div className="flex flex-wrap gap-2">
                {resultText(shown.result, "mail_url") && (
                  <Button size="sm" asChild>
                    <a href={resultText(shown.result, "mail_url")!} target="_blank" rel="noreferrer">
                      Open in Gmail
                    </a>
                  </Button>
                )}
                <Button size="sm" variant="outline" disabled={pending}
                  onClick={() => act(() => api.regenerateDraft(task.id), "Rewriting the draft")}>
                  Regenerate
                </Button>
                <Button size="sm" variant="outline" disabled={pending}
                  onClick={() => act(() => api.discardDraft(task.id), "Draft discarded")}>
                  Discard
                </Button>
              </div>
            </div>
          )}

          {shown.state === "failed" && (
            <div className="space-y-2">
              <p className="text-xs text-muted-foreground">{resultText(shown.result, "error") ?? "The agent failed."}</p>
              <Button size="sm" variant="outline" disabled={pending} onClick={() => start(shown.kind, shown.brief)}>
                Try again
              </Button>
            </div>
          )}
        </div>
      )}

      {!handoffActive(handoff) && task.status === "open" && (
        menuOpen ? (
          <div className="space-y-2 rounded-md border px-3 py-2">
            <Input value={brief} onChange={(event) => setBrief(event.target.value)}
              placeholder="What should the agent do? (optional)" aria-label="Agent brief" />
            <div className="flex flex-wrap gap-2">
              {manualHandoffKinds(task).map((kind) => (
                <Button key={kind} size="sm" variant="outline" disabled={pending} onClick={() => start(kind)}>
                  {HANDOFF_KIND_LABEL[kind]}
                </Button>
              ))}
              <Button size="sm" variant="ghost" onClick={() => setMenuOpen(false)}>Cancel</Button>
            </div>
          </div>
        ) : (
          <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={() => setMenuOpen(true)}>
            <Bot className="mr-1 h-3.5 w-3.5" /> Hand off…
          </Button>
        )
      )}
    </section>
  );
}
