import { ListTodo } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import type { ChatCitation } from "@/lib/types";

// A small inline renderer for streamed assistant text. Unlike the block-level
// Markdown component, this keeps citation chips ([T1]) and widgets inline, and
// resolves them to their retrieved hit (open a task, or the source URL).
//
// Widgets the model emits (no tool call needed):
//   • task:{<id>}  → a clickable task card, resolved from the cited hits.
//   • loc:{<place>} → a Google Maps link.

interface Rule {
  re: RegExp;
  render: (m: RegExpExecArray, key: string, ctx: Ctx) => ReactNode;
}

interface Ctx {
  byTag: Map<string, ChatCitation>;
  byTaskId: Map<string, ChatCitation>;
  onOpenTask: (taskId: string) => void;
  // Reveal a citation's content when it has no navigable target (notes, or an
  // input without a source link).
  onShowContent: (cite: ChatCitation) => void;
}

function mapsUrl(place: string): string {
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(place)}`;
}

const RULES: Rule[] = [
  {
    re: /task:\{([^}]+)\}/,
    render: (m, key, ctx) => <TaskCard key={key} taskId={m[1].trim()} ctx={ctx} />,
  },
  {
    re: /loc:\{([^}]+)\}/,
    render: (m, key) => {
      const place = m[1].trim();
      return (
        <a
          key={key}
          href={mapsUrl(place)}
          target="_blank"
          rel="noopener noreferrer"
          className="text-primary underline underline-offset-2"
        >
          {place}
        </a>
      );
    },
  },
  // Markdown link — before the citation rule so `[x](url)` never reads as one.
  {
    re: /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/,
    render: (m, key) => (
      <a
        key={key}
        href={m[2]}
        target="_blank"
        rel="noopener noreferrer"
        className="text-primary underline underline-offset-2"
      >
        {m[1]}
      </a>
    ),
  },
  {
    // One or more tags in a single bracket, e.g. [N2] or [N2, N4] → a chip each.
    re: /\[([A-Z]\d+(?:\s*,\s*[A-Z]\d+)*)\]/,
    render: (m, key, ctx) => {
      const tags = m[1].split(",").map((t) => t.trim()).filter(Boolean);
      return (
        <span key={key} className="whitespace-nowrap">
          {tags.map((t, j) => (
            <CitationChip key={j} tag={t} ctx={ctx} />
          ))}
        </span>
      );
    },
  },
  {
    re: /`([^`]+)`/,
    render: (m, key) => (
      <code key={key} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">
        {m[1]}
      </code>
    ),
  },
  {
    re: /\*\*([^*]+)\*\*/,
    render: (m, key) => <strong key={key}>{m[1]}</strong>,
  },
];

function renderInline(text: string, prefix: string, ctx: Ctx): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let i = 0;
  while (rest) {
    let best: { rule: Rule; m: RegExpExecArray } | null = null;
    for (const rule of RULES) {
      const m = rule.re.exec(rest);
      if (m && (!best || m.index < best.m.index)) best = { rule, m };
    }
    if (!best) {
      out.push(rest);
      break;
    }
    if (best.m.index > 0) out.push(rest.slice(0, best.m.index));
    out.push(best.rule.render(best.m, `${prefix}-${i++}`, ctx));
    rest = rest.slice(best.m.index + best.m[0].length);
  }
  return out;
}

function CitationChip({ tag, ctx }: { tag: string; ctx: Ctx }) {
  const cite = ctx.byTag.get(tag);
  const openTask = cite?.task_id ?? (cite?.type === "task" ? cite.id : null);
  const openUrl = openTask ? null : (cite?.url ?? null);
  const canShow = Boolean(cite && (openTask || openUrl || cite.snippet));
  const activate = () => {
    if (!cite) return;
    if (openTask) ctx.onOpenTask(openTask);
    else if (openUrl) window.open(openUrl, "_blank", "noopener,noreferrer");
    else ctx.onShowContent(cite);
  };
  return (
    <button
      type="button"
      disabled={!canShow}
      onClick={canShow ? activate : undefined}
      title={cite?.title ?? tag}
      className={cn(
        "mx-0.5 inline-flex h-4 translate-y-[-1px] items-center rounded px-1 align-middle text-[10px] font-semibold",
        canShow
          ? "cursor-pointer bg-primary/15 text-primary hover:bg-primary/25"
          : "cursor-default bg-muted text-muted-foreground",
      )}
    >
      {tag}
    </button>
  );
}

function TaskCard({ taskId, ctx }: { taskId: string; ctx: Ctx }) {
  const cite = ctx.byTaskId.get(taskId);
  const title = cite?.title ?? "Open task";
  const status = cite?.status ?? null;
  return (
    <button
      type="button"
      onClick={() => ctx.onOpenTask(taskId)}
      className="my-1 inline-flex max-w-full items-center gap-2 rounded-xl border bg-card px-3 py-2 text-left align-middle text-sm shadow-sm transition-colors hover:border-primary/40 hover:bg-accent"
    >
      <ListTodo className="h-4 w-4 shrink-0 text-muted-foreground" />
      <span className="min-w-0 truncate font-medium">{title}</span>
      {status && (
        <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
          {status}
        </span>
      )}
    </button>
  );
}

export function AssistantContent({
  content,
  citations,
  onOpenTask,
  onShowContent,
}: {
  content: string;
  citations: ChatCitation[];
  onOpenTask: (taskId: string) => void;
  onShowContent: (cite: ChatCitation) => void;
}) {
  const byTaskId = new Map<string, ChatCitation>();
  for (const c of citations) {
    if (c.type === "task") byTaskId.set(c.id, c);
    if (c.task_id) byTaskId.set(c.task_id, c);
  }
  const ctx: Ctx = {
    byTag: new Map(citations.map((c) => [c.tag, c])),
    byTaskId,
    onOpenTask,
    onShowContent,
  };

  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let key = 0;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i]))
        items.push(lines[i++].replace(/^\s*[-*]\s+/, ""));
      blocks.push(
        <ul key={key++} className="list-disc space-y-1 pl-5">
          {items.map((it, j) => (
            <li key={j}>{renderInline(it, `ul${key}-${j}`, ctx)}</li>
          ))}
        </ul>,
      );
      continue;
    }
    blocks.push(
      <p key={key++} className="whitespace-pre-wrap">
        {renderInline(line, `p${key}`, ctx)}
      </p>,
    );
    i++;
  }
  return <div className="space-y-2 break-words text-[15px] leading-relaxed">{blocks}</div>;
}
