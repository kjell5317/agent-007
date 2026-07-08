import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { ListTodo } from "lucide-react";
import { TaskCard } from "@/components/tasks/TaskCard";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { ChatCitation, Task } from "@/lib/types";

// A small inline renderer for streamed assistant text. Unlike the block-level
// Markdown component, this keeps citation chips ([T1]) and widgets inline, and
// resolves them to their retrieved hit (open a task, or the source URL).
//
// Widgets the model emits (no tool call needed):
//   • task:{<id>}  → a full task card (the same one the task view renders),
//     pulled block-level out of the text flow and fetched by id.
//   • loc:{<place>} → a Google Maps link.
//
// A task shown as a card makes its own citation redundant, so any citation
// chip pointing at a carded task is suppressed.

interface Rule {
  re: RegExp;
  render: (m: RegExpExecArray, key: string, ctx: Ctx) => ReactNode;
}

interface Ctx {
  byTag: Map<string, ChatCitation>;
  byTaskId: Map<string, ChatCitation>;
  suppressedTags: Set<string>;
  onOpenTask: (taskId: string) => void;
  // Reveal a citation's content when it has no navigable target (notes, or an
  // input without a source link).
  onShowContent: (cite: ChatCitation) => void;
}

const TASK_WIDGET = /task:\{([^}]+)\}/;

function mapsUrl(place: string): string {
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(place)}`;
}

// Inline rules. The task widget is handled at block level (a card is a block
// element and can't live inside a <p>), so it is intentionally absent here.
const RULES: Rule[] = [
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
      const chips = tags
        .filter((t) => !ctx.suppressedTags.has(t))
        .map((t, j) => <CitationChip key={j} tag={t} ctx={ctx} />);
      if (chips.length === 0) return null;
      return (
        <span key={key} className="whitespace-nowrap">
          {chips}
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

// Split a line into text runs (rendered inline in a <p>) and task cards
// (rendered as block elements). Text runs that are empty or only leftover
// punctuation — e.g. a stray "." after a suppressed citation — are dropped so
// a card isn't trailed by a fragment.
function renderTaskLine(text: string, prefix: string, ctx: Ctx): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let i = 0;
  while (rest) {
    const m = TASK_WIDGET.exec(rest);
    if (!m) {
      pushText(out, rest, `${prefix}-${i++}`, ctx);
      break;
    }
    if (m.index > 0) pushText(out, rest.slice(0, m.index), `${prefix}-${i++}`, ctx);
    out.push(
      <div key={`${prefix}-${i++}`} className="my-1.5">
        <ChatTaskCard taskId={m[1].trim()} ctx={ctx} />
      </div>,
    );
    rest = rest.slice(m.index + m[0].length);
  }
  return out;
}

function pushText(out: ReactNode[], text: string, key: string, ctx: Ctx): void {
  const trimmed = text.trim();
  if (!trimmed || /^[\s.,;:—–-]+$/.test(trimmed)) return;
  out.push(
    <p key={key} className="whitespace-pre-wrap">
      {renderInline(text, key, ctx)}
    </p>,
  );
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

// Fetches the full task by id and renders the same card the task view uses.
// While loading: a skeleton; if the task can't be loaded (e.g. deleted): a
// compact clickable fallback pill so the reference isn't lost.
function ChatTaskCard({ taskId, ctx }: { taskId: string; ctx: Ctx }) {
  const [task, setTask] = useState<Task | null>(null);
  const [failed, setFailed] = useState(false);

  const refetch = useCallback(async () => {
    try {
      setTask(await api.getTask(taskId));
    } catch {
      setFailed(true);
    }
  }, [taskId]);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  if (failed) {
    const title = ctx.byTaskId.get(taskId)?.title ?? "Open task";
    return (
      <button
        type="button"
        onClick={() => ctx.onOpenTask(taskId)}
        className="inline-flex max-w-full items-center gap-2 rounded-xl border bg-card px-3 py-2 text-left text-sm shadow-sm transition-colors hover:border-primary/40 hover:bg-accent"
      >
        <ListTodo className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 truncate font-medium">{title}</span>
      </button>
    );
  }

  if (!task) {
    return <div className="h-[3.25rem] animate-pulse rounded-xl border bg-muted/40" />;
  }

  return (
    <TaskCard
      task={task}
      kotxTask={null}
      onChanged={refetch}
      onKotxChanged={refetch}
      onOpen={ctx.onOpenTask}
    />
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

  // Every task rendered as a card in this message.
  const cardedTaskIds = new Set(
    [...content.matchAll(/task:\{([^}]+)\}/g)].map((m) => m[1].trim()),
  );
  // A citation pointing at a carded task is redundant with the card, so hide it.
  const suppressedTags = new Set<string>();
  for (const c of citations) {
    const target = c.task_id ?? (c.type === "task" ? c.id : null);
    if (target && cardedTaskIds.has(target)) suppressedTags.add(c.tag);
  }

  const ctx: Ctx = {
    byTag: new Map(citations.map((c) => [c.tag, c])),
    byTaskId,
    suppressedTags,
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
    const isBullet = /^\s*[-*]\s+/.test(line);
    const hasTask = TASK_WIDGET.test(line);
    // Bullet run — but only lines without a task widget; a widget breaks out
    // into its own block card below.
    if (isBullet && !hasTask) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i]) && !TASK_WIDGET.test(lines[i]))
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
    if (hasTask) {
      // Drop any leading bullet marker; the card stands on its own.
      blocks.push(...renderTaskLine(line.replace(/^\s*[-*]\s+/, ""), `tl${key++}`, ctx));
      i++;
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
