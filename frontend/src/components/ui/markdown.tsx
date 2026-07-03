import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

// A deliberately small CommonMark subset — enough for the agent-generated
// TASK.md / REVIEW.md / prompt docs (headings, lists, code, blockquotes,
// emphasis, links). Rendered as React elements (never raw HTML) and links
// are restricted to http(s)/relative, so untrusted content can't inject.

interface InlineRule {
  re: RegExp;
  render: (m: RegExpExecArray, key: string) => ReactNode;
}

const INLINE: InlineRule[] = [
  {
    re: /`([^`]+)`/,
    render: (m, key) => (
      <code key={key} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">
        {m[1]}
      </code>
    ),
  },
  {
    re: /\[([^\]]+)\]\(([^)\s]+)\)/,
    render: (m, key) => safeLink(m[1], m[2], key),
  },
  {
    re: /\*\*([^*]+)\*\*/,
    render: (m, key) => <strong key={key}>{renderInline(m[1], key)}</strong>,
  },
  {
    re: /\*([^*]+)\*/,
    render: (m, key) => <em key={key}>{renderInline(m[1], key)}</em>,
  },
];

// Single source of truth for fenced-code styling — shared with raw tool-call
// input so code renders identically whether embedded in markdown or standalone.
export const CODE_BLOCK_CLASS =
  "overflow-auto rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-relaxed";

function safeLink(text: string, href: string, key: string): ReactNode {
  if (!/^(https?:\/\/|\/)/.test(href)) return <span key={key}>{text}</span>;
  return (
    <a
      key={key}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-primary underline underline-offset-2"
    >
      {renderInline(text, key)}
    </a>
  );
}

function renderInline(text: string, prefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let i = 0;
  while (rest) {
    let best: { rule: InlineRule; m: RegExpExecArray } | null = null;
    for (const rule of INLINE) {
      const m = rule.re.exec(rest);
      if (m && (!best || m.index < best.m.index)) best = { rule, m };
    }
    if (!best) {
      out.push(rest);
      break;
    }
    if (best.m.index > 0) out.push(rest.slice(0, best.m.index));
    out.push(best.rule.render(best.m, `${prefix}-${i++}`));
    rest = rest.slice(best.m.index + best.m[0].length);
  }
  return out;
}

function isBlockStart(line: string): boolean {
  return (
    /^```/.test(line) ||
    /^#{1,6}\s/.test(line) ||
    /^(-{3,}|\*{3,}|_{3,})$/.test(line.trim()) ||
    /^>\s?/.test(line) ||
    /^\s*[-*+]\s+/.test(line) ||
    /^\s*\d+\.\s+/.test(line)
  );
}

function heading(level: number, text: string, key: string): ReactNode {
  const inner = renderInline(text, key);
  if (level === 1) return <h1 key={key} className="mt-1 text-base font-semibold">{inner}</h1>;
  if (level === 2) return <h2 key={key} className="mt-1 text-sm font-semibold">{inner}</h2>;
  return (
    <h3 key={key} className="mt-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
      {inner}
    </h3>
  );
}

function parseBlocks(src: string): ReactNode[] {
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    if (/^```/.test(line)) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++; // skip closing fence
      out.push(
        <pre key={k++} className={CODE_BLOCK_CLASS}>
          <code>{buf.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      out.push(heading(h[1].length, h[2], `h${k++}`));
      i++;
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      out.push(<hr key={k++} className="border-t" />);
      i++;
      continue;
    }
    if (/^>\s?/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
      out.push(
        <blockquote key={k++} className="border-l-2 border-border pl-3 text-muted-foreground">
          {parseBlocks(buf.join("\n"))}
        </blockquote>,
      );
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i]))
        items.push(lines[i++].replace(/^\s*[-*+]\s+/, ""));
      out.push(
        <ul key={k++} className="list-disc space-y-1 pl-5">
          {items.map((it, j) => (
            <li key={j}>{renderInline(it, `ul${k}-${j}`)}</li>
          ))}
        </ul>,
      );
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i]))
        items.push(lines[i++].replace(/^\s*\d+\.\s+/, ""));
      out.push(
        <ol key={k++} className="list-decimal space-y-1 pl-5">
          {items.map((it, j) => (
            <li key={j}>{renderInline(it, `ol${k}-${j}`)}</li>
          ))}
        </ol>,
      );
      continue;
    }
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) buf.push(lines[i++]);
    out.push(<p key={k++}>{renderInline(buf.join(" "), `p${k}`)}</p>);
  }
  return out;
}

export function Markdown({ content, className }: { content: string; className?: string }) {
  return (
    <div className={cn("space-y-2 break-words text-sm leading-relaxed", className)}>
      {parseBlocks(content)}
    </div>
  );
}
