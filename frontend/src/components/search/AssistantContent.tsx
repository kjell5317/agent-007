import {
  Children,
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useState,
} from "react";
import type { ComponentType, ReactNode } from "react";
import {
  BookOpen,
  CalendarDays,
  Cake,
  ExternalLink,
  FileText,
  ListTodo,
  Mail,
  MapPin,
  Phone,
  UserRound,
} from "lucide-react";
import { TaskCard } from "@/components/tasks/TaskCard";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import { subscribeEvents } from "@/lib/events";
import { cn } from "@/lib/utils";
import type { ChatCitation, ChatCitationMeta, Task } from "@/lib/types";

// A small inline renderer for streamed assistant text. Unlike the block-level
// Markdown component, this keeps citation chips ([T1]) and inline widgets
// (loc:{}, Notion links) inline, and pulls card widgets out to their own block.
//
// Card widgets the model emits (no tool call needed), rendered block-level so
// they never split a sentence:
//   • task:{<id>}     → a full task card (fetched live by id)
//   • contact:{<C#>}  → a contact card (from the cited hit)
//   • event:{<E#>}    → a calendar-event card
//   • doc:{<D#|G#>}   → a document / Drive file card
// Inline widgets:
//   • loc:{<place>}   → a Google Maps link
//   • a notion.so link → a Notion page chip
//
// An item shown as a widget makes its own citation chip redundant, so a chip
// pointing at it is suppressed.

interface Rule {
  re: RegExp;
  render: (m: RegExpExecArray, key: string, ctx: Ctx) => ReactNode;
}

interface Ctx {
  byTag: Map<string, ChatCitation>;
  byTaskId: Map<string, ChatCitation>;
  suppressedTags: Set<string>;
  // Normalized titles of items shown as cards. The card already shows the
  // title, so a standalone line repeating it (the model often emits the widget
  // AND the title) is dropped.
  cardedTitles: Set<string>;
  onOpenTask: (taskId: string) => void;
  // Reveal a citation's content when it has no navigable target (notes, or an
  // input without a source link).
  onShowContent: (cite: ChatCitation) => void;
}

type WidgetKind = "task" | "contact" | "event" | "doc";

// Card widgets, pulled to their own block. `loc:` stays inline (a map link).
const BLOCK_WIDGET = /(task|contact|event|doc):\{([^}]+)\}/;
const BLOCK_WIDGET_G = /(task|contact|event|doc):\{([^}]+)\}/g;
const HEADING = /^(#{1,6})\s+(.+)$/;
const BULLET = /^\s*[-*]\s+/;
const ORDERED = /^\s*\d+\.\s+/;

// Normalize for title-equality: strip markdown bold, surrounding markup, and
// trailing sentence punctuation; lowercase; collapse whitespace.
function normalizeTitle(text: string): string {
  return text
    .replace(/\*\*/g, "")
    .replace(/[.,;:]+$/, "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

function isDuplicateTitle(text: string, ctx: Ctx): boolean {
  const n = normalizeTitle(text);
  return n.length > 0 && ctx.cardedTitles.has(n);
}

function mapsUrl(place: string): string {
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(place)}`;
}

function isNotionUrl(url: string): boolean {
  return /^https?:\/\/([a-z0-9-]+\.)*(notion\.so|notion\.site)\//i.test(url);
}

// Inline rules. Card widgets are handled at block level (a card can't live
// inside a <p>), so they are intentionally absent here.
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
  // A Notion link renders as a compact page chip instead of a bare link.
  {
    re: /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/,
    render: (m, key) =>
      isNotionUrl(m[2]) ? (
        <NotionChip key={key} href={m[2]} label={m[1]} />
      ) : (
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
  // A bare Notion URL (no markdown link wrapper).
  {
    re: /(https?:\/\/(?:[a-z0-9-]+\.)*(?:notion\.so|notion\.site)\/[^\s)]+)/i,
    render: (m, key) => <NotionChip key={key} href={m[1]} label="Notion page" />,
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
  {
    // Italic: a single *…* that isn't bold. Bold (**…**) always matches at a
    // lower index, so it wins the earliest-match tiebreak; requiring a
    // non-space first char keeps stray asterisks ("2 * 3") from emphasizing.
    re: /\*([^*\s][^*\n]*?)\*/,
    render: (m, key) => <em key={key}>{m[1]}</em>,
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

// A blinking caret appended to the end of the streaming answer.
function Caret() {
  return (
    <span
      aria-hidden
      className="animate-caret-blink ml-0.5 inline-block h-[1.05em] w-[2px] translate-y-[0.15em] rounded-[1px] bg-foreground/70 align-baseline"
    />
  );
}

// A line that holds one or more card widgets. The widgets are pulled out to
// their own blocks so they never land in the middle of a text block; the
// surrounding text renders as a single paragraph BEFORE them.
function renderWidgetLine(text: string, prefix: string, ctx: Ctx): ReactNode[] {
  const out: ReactNode[] = [];
  const widgets: { kind: WidgetKind; value: string }[] = [];
  let stripped = "";
  let last = 0;
  for (const m of text.matchAll(BLOCK_WIDGET_G)) {
    stripped += text.slice(last, m.index);
    widgets.push({ kind: m[1] as WidgetKind, value: m[2].trim() });
    last = (m.index ?? 0) + m[0].length;
  }
  stripped += text.slice(last);

  pushText(out, stripped, `${prefix}-t`, ctx);
  widgets.forEach((w, j) => {
    out.push(
      <div key={`${prefix}-w${j}`} className="my-1.5">
        {renderWidget(w, ctx)}
      </div>,
    );
  });
  return out;
}

// The value inside a widget token. The model is told to pass an `id` for tasks
// and a `[C#]`-style tag for the rest; tolerate stray brackets/spaces either way.
function widgetKey(value: string): string {
  return value.replace(/[[\]\s]/g, "");
}

function renderWidget(w: { kind: WidgetKind; value: string }, ctx: Ctx): ReactNode {
  const key = widgetKey(w.value);
  if (w.kind === "task") {
    // Prefer a real id; if the model passed a citation tag, resolve it.
    const cite = ctx.byTag.get(key);
    return <ChatTaskCard taskId={cite ? (cite.task_id ?? cite.id) : key} ctx={ctx} />;
  }
  const cite = ctx.byTag.get(key);
  if (!cite) return <FallbackChip label={key} />;
  if (w.kind === "contact") return <ContactCard cite={cite} />;
  if (w.kind === "event") return <EventCard cite={cite} ctx={ctx} />;
  return <DocCard cite={cite} ctx={ctx} />;
}

function pushText(out: ReactNode[], text: string, key: string, ctx: Ctx): void {
  const trimmed = text.trim();
  if (!trimmed || /^[\s.,;:—–-]+$/.test(trimmed)) return;
  // The adjacent card already shows this title — don't repeat it as text.
  if (isDuplicateTitle(text, ctx)) return;
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

// --- Citation card widgets ---------------------------------------------------

function citeMeta(cite: ChatCitation): ChatCitationMeta {
  return (cite.meta ?? {}) as ChatCitationMeta;
}

// Shared shell: an icon, a title, optional detail rows, and (when the citation
// has a URL) an open-in-new affordance. The whole card opens the source.
function WidgetShell({
  Icon,
  title,
  href,
  onActivate,
  children,
}: {
  Icon: ComponentType<{ className?: string }>;
  title: string;
  href?: string | null;
  onActivate?: () => void;
  children?: ReactNode;
}) {
  const clickable = Boolean(href || onActivate);
  const activate = () => {
    if (href) window.open(href, "_blank", "noopener,noreferrer");
    else onActivate?.();
  };
  return (
    <div
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={clickable ? activate : undefined}
      onKeyDown={
        clickable
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                activate();
              }
            }
          : undefined
      }
      className={cn(
        "flex max-w-full items-start gap-3 rounded-xl border bg-card px-3 py-2.5 text-left shadow-sm transition-colors",
        clickable && "cursor-pointer hover:border-primary/40 hover:bg-accent",
      )}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="min-w-0 flex-1 truncate text-sm font-medium">{title}</span>
          {href && <ExternalLink className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
        </div>
        {children}
      </div>
    </div>
  );
}

function DetailRow({
  Icon,
  children,
}: {
  Icon: ComponentType<{ className?: string }>;
  children: ReactNode;
}) {
  return (
    <div className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
      <Icon className="h-3 w-3 shrink-0" />
      <span className="min-w-0 truncate">{children}</span>
    </div>
  );
}

function ContactCard({ cite }: { cite: ChatCitation }) {
  const meta = citeMeta(cite);
  const emails = meta.emails ?? [];
  const phones = meta.phones ?? [];
  const address = meta.addresses?.[0];
  return (
    <WidgetShell Icon={UserRound} title={cite.title || "Contact"} href={cite.url}>
      {meta.org && <div className="mt-0.5 truncate text-xs text-muted-foreground">{meta.org}</div>}
      {emails.length > 0 && <DetailRow Icon={Mail}>{emails.join(", ")}</DetailRow>}
      {phones.length > 0 && <DetailRow Icon={Phone}>{phones.join(", ")}</DetailRow>}
      {meta.birthday && <DetailRow Icon={Cake}>{meta.birthday}</DetailRow>}
      {address && <DetailRow Icon={MapPin}>{address}</DetailRow>}
    </WidgetShell>
  );
}

function EventCard({ cite, ctx }: { cite: ChatCitation; ctx: Ctx }) {
  const meta = citeMeta(cite);
  const when = fmtWhen(meta.start ?? cite.ts ?? null);
  const location = meta.location ?? null;
  const onActivate = cite.url ? undefined : () => ctx.onShowContent(cite);
  return (
    <WidgetShell
      Icon={CalendarDays}
      title={cite.title || "Event"}
      href={cite.url}
      onActivate={onActivate}
    >
      {when && <DetailRow Icon={CalendarDays}>{when}</DetailRow>}
      {location && <DetailRow Icon={MapPin}>{location}</DetailRow>}
    </WidgetShell>
  );
}

function DocCard({ cite, ctx }: { cite: ChatCitation; ctx: Ctx }) {
  const meta = citeMeta(cite);
  const onActivate = cite.url ? undefined : () => ctx.onShowContent(cite);
  return (
    <WidgetShell Icon={FileText} title={cite.title || "Document"} href={cite.url} onActivate={onActivate}>
      {meta.mime && <div className="mt-0.5 truncate text-xs text-muted-foreground">{meta.mime}</div>}
    </WidgetShell>
  );
}

// Inline Notion page chip — a compact link, since Notion references usually sit
// mid-sentence rather than on their own line.
function NotionChip({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="mx-0.5 inline-flex max-w-full items-center gap-1 rounded-md border bg-card px-1.5 py-0.5 align-middle text-[13px] font-medium text-foreground transition-colors hover:border-primary/40 hover:bg-accent"
    >
      <BookOpen className="h-3 w-3 shrink-0 text-muted-foreground" />
      <span className="truncate">{label}</span>
    </a>
  );
}

// Widget whose citation couldn't be resolved (dropped tag) — keep the reference
// visible rather than rendering nothing.
function FallbackChip({ label }: { label: string }) {
  return (
    <div className="inline-flex items-center gap-2 rounded-xl border bg-card px-3 py-2 text-sm text-muted-foreground shadow-sm">
      <FileText className="h-4 w-4 shrink-0" />
      <span className="truncate">{label}</span>
    </div>
  );
}

// Fetches the full task by id and renders the same card the task view uses.
// While loading: a skeleton; if the task can't be loaded (e.g. deleted): a
// compact clickable fallback pill so the reference isn't lost.
function ChatTaskCard({ taskId, ctx }: { taskId: string; ctx: Ctx }) {
  const [task, setTask] = useState<Task | null>(null);
  const [failed, setFailed] = useState(false);

  const refetch = useCallback(async (opts: { background?: boolean } = {}) => {
    try {
      const next = await api.getTask(taskId);
      setTask(next);
      setFailed(false);
    } catch (e) {
      if (opts.background && apiStatus(e) !== 404) return;
      setTask(null);
      setFailed(true);
    }
  }, [taskId]);

  useEffect(() => {
    setTask(null);
    setFailed(false);
    void refetch();
  }, [refetch]);

  useEffect(() => {
    return subscribeEvents((event) => {
      if (event.type === "task" && event.data.id === taskId) {
        setTask(event.data);
        setFailed(false);
      } else if (event.type === "task_removed" && event.id === taskId) {
        setTask(null);
        setFailed(true);
      }
    });
  }, [taskId]);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;

    const safeRefetch = async () => {
      if (inFlight || cancelled) return;
      inFlight = true;
      try {
        await refetch({ background: true });
      } catch {
        // `refetch` owns state reconciliation; foreground refreshes stay quiet.
      } finally {
        inFlight = false;
      }
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") safeRefetch();
    };

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", safeRefetch);

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", safeRefetch);
    };
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

function apiStatus(e: unknown): number | null {
  if (!e || typeof e !== "object" || !("status" in e)) return null;
  const status = (e as { status: unknown }).status;
  return typeof status === "number" ? status : null;
}

export function AssistantContent({
  content,
  citations,
  caret = false,
  onOpenTask,
  onShowContent,
}: {
  content: string;
  citations: ChatCitation[];
  // Append a blinking caret to the end of the answer while it streams.
  caret?: boolean;
  onOpenTask: (taskId: string) => void;
  onShowContent: (cite: ChatCitation) => void;
}) {
  const byTag = new Map(citations.map((c) => [c.tag, c]));
  const byTaskId = new Map<string, ChatCitation>();
  for (const c of citations) {
    if (c.type === "task") byTaskId.set(c.id, c);
    if (c.task_id) byTaskId.set(c.task_id, c);
  }

  // Every widget rendered as a card in this message. Resolve each to the task
  // id / citation tag it cards, tolerating a tag passed where an id was asked.
  const cardedTaskIds = new Set<string>();
  const cardedTags = new Set<string>();
  for (const m of content.matchAll(BLOCK_WIDGET_G)) {
    const kind = m[1] as WidgetKind;
    const key = widgetKey(m[2]);
    if (kind === "task") {
      const cite = byTag.get(key);
      cardedTaskIds.add(cite ? (cite.task_id ?? cite.id) : key);
      if (cite) cardedTags.add(cite.tag); // also drop the [T#] chip for it
    } else {
      cardedTags.add(key);
    }
  }

  // A citation shown as a card widget is redundant with the card, so hide its
  // inline chip and drop any line that just repeats its title.
  const suppressedTags = new Set<string>();
  const cardedTitles = new Set<string>();
  for (const c of citations) {
    const target = c.task_id ?? (c.type === "task" ? c.id : null);
    const carded = cardedTags.has(c.tag) || (target != null && cardedTaskIds.has(target));
    if (carded) {
      suppressedTags.add(c.tag);
      if (c.title) cardedTitles.add(normalizeTitle(c.title));
    }
  }

  const ctx: Ctx = {
    byTag,
    byTaskId,
    suppressedTags,
    cardedTitles,
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
    const hasWidget = BLOCK_WIDGET.test(line);
    // Heading (`## …`) — a compact bold line; inline markup inside still renders.
    const heading = !hasWidget ? HEADING.exec(line) : null;
    if (heading) {
      if (!isDuplicateTitle(heading[2], ctx)) {
        blocks.push(
          <p
            key={key++}
            className={cn(
              "font-semibold",
              heading[1].length <= 2 ? "text-base" : "text-sm",
            )}
          >
            {renderInline(heading[2], `h${key}`, ctx)}
          </p>,
        );
      }
      i++;
      continue;
    }
    // List runs — bullet (`-`/`*`) or ordered (`1.`), but only lines without a
    // card widget; a widget breaks out into its own block below.
    const listMarker = BULLET.test(line) ? BULLET : ORDERED.test(line) ? ORDERED : null;
    if (listMarker && !hasWidget) {
      const items: string[] = [];
      while (i < lines.length && listMarker.test(lines[i]) && !BLOCK_WIDGET.test(lines[i]))
        items.push(lines[i++].replace(listMarker, ""));
      // Drop items that just repeat a carded item's title.
      const kept = items.filter((it) => !isDuplicateTitle(it, ctx));
      if (kept.length > 0) {
        const ListTag = listMarker === ORDERED ? "ol" : "ul";
        blocks.push(
          <ListTag
            key={key++}
            className={cn(
              "space-y-1 pl-5",
              listMarker === ORDERED ? "list-decimal" : "list-disc",
            )}
          >
            {kept.map((it, j) => (
              <li key={j}>{renderInline(it, `li${key}-${j}`, ctx)}</li>
            ))}
          </ListTag>,
        );
      }
      continue;
    }
    if (hasWidget) {
      // Drop any leading bullet marker; the card stands on its own.
      blocks.push(...renderWidgetLine(line.replace(/^\s*[-*]\s+/, ""), `wl${key++}`, ctx));
      i++;
      continue;
    }
    // A standalone line that just repeats a carded item's title is redundant.
    if (isDuplicateTitle(line, ctx)) {
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

  if (caret) appendCaret(blocks);

  return <div className="space-y-2 break-words text-[15px] leading-relaxed">{blocks}</div>;
}

// Attach the streaming caret inline to the final text block (a <p>, incl.
// headings). Falls back to its own line after a non-text block (list/card).
function appendCaret(blocks: ReactNode[]): void {
  const last = blocks[blocks.length - 1];
  if (isValidElement(last) && last.type === "p") {
    const kids = Children.toArray((last.props as { children?: ReactNode }).children);
    blocks[blocks.length - 1] = cloneElement(last, undefined, ...kids, <Caret key="caret" />);
  } else {
    blocks.push(
      <p key="caret" className="whitespace-pre-wrap">
        <Caret />
      </p>,
    );
  }
}
