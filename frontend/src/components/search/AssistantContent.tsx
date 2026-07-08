import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import type { ChatCitation } from "@/lib/types";

// A small inline renderer for streamed assistant text. Unlike the block-level
// Markdown component, this keeps citation chips ([T1]) inline mid-sentence and
// resolves them to their retrieved hit (open a task, or the source URL).

interface Rule {
  re: RegExp;
  render: (m: RegExpExecArray, key: string, ctx: Ctx) => ReactNode;
}

interface Ctx {
  byTag: Map<string, ChatCitation>;
  onOpenTask: (taskId: string) => void;
}

const RULES: Rule[] = [
  // Markdown link — checked before the citation rule so `[x](url)` never reads
  // as a citation.
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
    re: /\[([A-Z]\d+)\]/,
    render: (m, key, ctx) => <CitationChip key={key} tag={m[1]} ctx={ctx} />,
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
  const openUrl = cite?.url ?? null;
  const clickable = Boolean(openTask || openUrl);
  const activate = () => {
    if (openTask) ctx.onOpenTask(openTask);
    else if (openUrl) window.open(openUrl, "_blank", "noopener,noreferrer");
  };
  return (
    <button
      type="button"
      disabled={!clickable}
      onClick={clickable ? activate : undefined}
      title={cite?.title ?? tag}
      className={cn(
        "mx-0.5 inline-flex h-4 translate-y-[-1px] items-center rounded px-1 align-middle text-[10px] font-semibold",
        clickable
          ? "cursor-pointer bg-primary/15 text-primary hover:bg-primary/25"
          : "cursor-default bg-muted text-muted-foreground",
      )}
    >
      {tag}
    </button>
  );
}

export function AssistantContent({
  content,
  citations,
  onOpenTask,
}: {
  content: string;
  citations: ChatCitation[];
  onOpenTask: (taskId: string) => void;
}) {
  const ctx: Ctx = { byTag: new Map(citations.map((c) => [c.tag, c])), onOpenTask };
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
  return <div className="space-y-2 break-words text-sm leading-relaxed">{blocks}</div>;
}
