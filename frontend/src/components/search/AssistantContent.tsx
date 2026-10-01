import {
  Children,
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useState,
} from "react";
import type { ComponentType, ReactNode } from "react";
import { toast } from "sonner";
import {
  BookOpen,
  CalendarDays,
  Check,
  Copy,
  FileText,
  Globe,
  ListTodo,
  MapPin,
  UserRound,
} from "lucide-react";
import { TaskCard } from "@/components/tasks/TaskCard";
import { api } from "@/lib/api";
import { fmtWhen } from "@/lib/dates";
import { subscribeEvents } from "@/lib/events";
import { cn } from "@/lib/utils";
import type { ChatCitation, ChatCitationMeta, LinkPreview, Task } from "@/lib/types";

// A small inline renderer for streamed assistant text. Unlike the block-level
// Markdown component, this keeps inline widgets (loc:{}, Notion links) inline
// and pulls card widgets out to their own block. Bracketed citation tags ([T1])
// are stripped: the answer no longer shows citation chips (the card widgets
// still resolve their data from the citation array behind the scenes).
//
// Card widgets the model emits (no tool call needed), rendered block-level so
// they never split a sentence:
//   • task:{<id>}     → a full task card (fetched live by id)
//   • contact:{<C#>}  → a contact card (from the cited hit)
//   • event:{<E#>}    → a calendar-event card
//   • doc:{<D#|G#>}   → a document / Drive file card
// Inline widgets:
//   • loc:{<place>}   → a Google Maps link
//   • copy:{<value>}  → copy a name, number, or identifier
//   • a notion.so link → a Notion page chip
//   • any other http(s) link → a fetched preview card (title/description)

interface Rule {
  re: RegExp;
  render: (m: RegExpExecArray, key: string, ctx: Ctx) => ReactNode;
}

interface Ctx {
  byTag: Map<string, ChatCitation>;
  bySourceId: Map<string, ChatCitation>;
  byTaskId: Map<string, ChatCitation>;
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
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
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

// Non-Notion http(s) URLs in a line — markdown-link targets and bare URLs.
// Notion links get their own chip, so they're skipped; the caller dedupes.
function collectPreviewUrls(text: string): string[] {
  const urls: string[] = [];
  const re = /\[[^\]]+\]\((https?:\/\/[^)\s]+)\)|(https?:\/\/[^\s)]+)/g;
  for (const m of text.matchAll(re)) {
    const u = (m[1] || m[2] || "").replace(/[.,;:!?)]+$/, "");
    if (u && !isNotionUrl(u)) urls.push(u);
  }
  return urls;
}

function safeHost(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

// Inline rules. Card widgets are handled at block level (a card can't live
// inside a <p>), so they are intentionally absent here.
const RULES: Rule[] = [
  {
    re: /loc:\{([^}]+)\}/,
    render: (m, key) => {
      const place = m[1].trim();
      if (/^<[^>]+>$/.test(place)) {
        return <code key={key} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">{m[0]}</code>;
      }
      return (
        <a
          key={key}
          href={mapsUrl(place)}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex max-w-full items-center gap-1 rounded-md border bg-card px-1.5 py-0.5 align-middle text-[0.9em] text-primary hover:bg-accent"
        >
          <MapPin className="h-3 w-3 shrink-0" />
          <span className="truncate">{place}</span>
        </a>
      );
    },
  },
  {
    re: /copy:\{([^}]+)\}/,
    render: (m, key) => /^<[^>]+>$/.test(m[1].trim())
      ? <code key={key} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">{m[0]}</code>
      : <CopyChip key={key} value={m[1].trim()} />,
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
    // Bracketed citation tags ([T1], [N2, N4]) — citation chips were removed, so
    // strip any the model still emits rather than leak literal brackets.
    re: /\s*\[([A-Z]\d+(?:\s*,\s*[A-Z]\d+)*)\]/,
    render: () => null,
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
    if (!isWidgetRef(m[1] as WidgetKind, widgetKey(m[2]))) continue;
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

function isWidgetRef(kind: WidgetKind, key: string): boolean {
  if (kind === "task") return UUID.test(key) || /^T\d+$/i.test(key);
  if (kind === "contact") return /^C\d+$/i.test(key) || /^people\/[^\s{}]+$/.test(key);
  if (kind === "event") return /^E\d+$/i.test(key);
  return /^[DG]\d+$/i.test(key) || (
    /^[A-Za-z0-9_-]{20,}$/.test(key) && !UUID.test(key)
  );
}

function hasBlockWidget(line: string): boolean {
  return [...line.matchAll(BLOCK_WIDGET_G)].some((m) =>
    isWidgetRef(m[1] as WidgetKind, widgetKey(m[2])),
  );
}

function renderWidget(w: { kind: WidgetKind; value: string }, ctx: Ctx): ReactNode {
  const key = widgetKey(w.value);
  if (w.kind === "task") {
    // Prefer a real id; if the model passed a citation tag, resolve it.
    const cite = ctx.byTag.get(key);
    return <ChatTaskCard taskId={cite ? (cite.task_id ?? cite.id) : key} ctx={ctx} />;
  }
  // Models sometimes use the source id from a search result instead of its
  // citation tag. Both identify the same retrieved item.
  const cite = ctx.byTag.get(key) ?? ctx.bySourceId.get(key);
  if (!cite && w.kind === "doc" && /^[A-Za-z0-9_-]{20,}$/.test(key)
    && !UUID.test(key)) {
    return <WidgetShell Icon={FileText} title={key} href={`https://drive.google.com/open?id=${encodeURIComponent(key)}`} />;
  }
  if (!cite) return <FallbackChip label={key} />;
  if (w.kind === "contact") return <ContactCard cite={cite} />;
  if (w.kind === "event") return <EventCard cite={cite} onShowContent={() => ctx.onShowContent(cite)} />;
  return <DocCard cite={cite} onShowContent={() => ctx.onShowContent(cite)} />;
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

// --- Citation card widgets ---------------------------------------------------

function citeMeta(cite: ChatCitation): ChatCitationMeta {
  return (cite.meta ?? {}) as ChatCitationMeta;
}

// Match the compact task card: one leading icon, one title, one line of pills.
function WidgetShell({
  Icon,
  title,
  href,
  onActivate,
  onOpened,
  pills = [],
}: {
  Icon: ComponentType<{ className?: string }>;
  title: string;
  href?: string | null;
  onActivate?: () => void;
  onOpened?: () => void;
  pills?: string[];
}) {
  const clickable = Boolean(href || onActivate);
  const activate = () => {
    if (href) window.open(href, "_blank", "noopener,noreferrer");
    else onActivate?.();
    onOpened?.();
  };
  return (
    <div
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={clickable ? activate : undefined}
      onKeyDown={
        clickable
          ? (e) => {
              if (e.target !== e.currentTarget) return;
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                activate();
              }
            }
          : undefined
      }
      className={cn(
        "flex h-[76px] max-w-full items-center gap-2 overflow-hidden rounded-xl border bg-card p-3 pl-2 text-left shadow-sm transition-colors",
        clickable && "cursor-pointer hover:border-primary/40 hover:bg-accent",
      )}
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center text-muted-foreground" aria-hidden="true">
        <Icon className="h-5 w-5" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-base font-medium leading-snug" title={title}>{title}</div>
        <div className="mt-1 flex min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap">
          {pills.filter(Boolean).map((pill, index) => (
            <span key={`${pill}:${index}`} title={pill} className="max-w-[50%] shrink-0 truncate rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
              {pill}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

export function ContactCard({ cite, onOpened }: { cite: ChatCitation; onOpened?: () => void }) {
  const meta = citeMeta(cite);
  const emails = meta.emails ?? [];
  const phones = meta.phones ?? [];
  const details = [meta.org, emails[0], phones[0], meta.birthday, meta.addresses?.[0]]
    .filter((value): value is string => Boolean(value));
  return (
    <WidgetShell Icon={UserRound} title={cite.title || "Contact"} href={cite.url} onOpened={onOpened} pills={details.slice(0, 2)} />
  );
}

export function EventCard({
  cite, onShowContent, onOpened,
}: { cite: ChatCitation; onShowContent?: () => void; onOpened?: () => void }) {
  const meta = citeMeta(cite);
  const when = fmtWhen(meta.start ?? cite.ts ?? null);
  const location = meta.location ?? null;
  const onActivate = cite.url ? undefined : onShowContent;
  return (
    <WidgetShell
      Icon={CalendarDays}
      title={cite.title || "Event"}
      href={cite.url}
      onActivate={onActivate}
      onOpened={onOpened}
      pills={[when, location].filter((value): value is string => Boolean(value))}
    />
  );
}

export function DocCard({
  cite, onShowContent, onOpened,
}: { cite: ChatCitation; onShowContent?: () => void; onOpened?: () => void }) {
  const meta = citeMeta(cite);
  const onActivate = cite.url ? undefined : onShowContent;
  return (
    <WidgetShell
      Icon={FileText}
      title={cite.title || "Document"}
      href={cite.url}
      onActivate={onActivate}
      onOpened={onOpened}
      pills={[cite.ts ? `Modified ${fmtWhen(cite.ts)}` : null, meta.mime].filter((value): value is string => Boolean(value))}
    />
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

function CopyChip({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      toast.error("Couldn't copy to clipboard");
    }
  };
  return (
    <button
      type="button"
      onClick={() => void copy()}
      aria-label={`Copy ${value}`}
      title={copied ? "Copied" : `Copy ${value}`}
      className="inline-flex max-w-full items-center gap-1 rounded-md border bg-card px-1.5 py-0.5 align-middle text-[0.9em] text-foreground hover:bg-accent"
    >
      {copied ? <Check className="h-3 w-3 shrink-0" /> : <Copy className="h-3 w-3 shrink-0" />}
      <span className="truncate">{value}</span>
    </button>
  );
}

// A fetched preview card for a plain http(s) link, pulled to its own block
// below the text (WhatsApp-style). While loading: a skeleton; if the URL can't
// be previewed: nothing (the inline link in the text still stands on its own).
function LinkPreviewCard({ url }: { url: string }) {
  const [preview, setPreview] = useState<LinkPreview | null>(null);
  const [done, setDone] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setPreview(null);
    setDone(false);
    api
      .getLinkPreview(url)
      .then((r) => {
        if (!cancelled) {
          setPreview(r.preview);
          setDone(true);
        }
      })
      .catch(() => {
        if (!cancelled) setDone(true);
      });
    return () => {
      cancelled = true;
    };
  }, [url]);

  if (!done) {
    return <div className="my-1.5 h-[76px] animate-pulse rounded-xl border bg-muted/40" />;
  }
  if (!preview) return null;

  return (
    <div className="my-1.5">
      <WidgetShell
        Icon={Globe}
        title={preview.title}
        href={url}
        pills={[preview.site_name || safeHost(url), preview.description].filter((value): value is string => Boolean(value))}
      />
    </div>
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
// If the task can't be loaded, keep a clickable reference to it.
function ChatTaskCard({ taskId, ctx }: { taskId: string; ctx: Ctx }) {
  const [task, setTask] = useState<Task | null>(null);
  const [failed, setFailed] = useState(false);

  const refetch = useCallback(async () => {
    try {
      const next = await api.getTask(taskId);
      setTask(next);
      setFailed(false);
    } catch {
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

  if (failed) {
    const title = ctx.byTaskId.get(taskId)?.title ?? "Open task";
    return <WidgetShell Icon={ListTodo} title={title} onActivate={() => ctx.onOpenTask(taskId)} />;
  }

  if (!task) {
    return null;
  }

  return (
    <TaskCard
      task={task}
      compact
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
  // A previous length-limited answer can end halfway through a task UUID.
  // Resolve it only when the prefix identifies exactly one cited task.
  const displayContent = caret ? content : content.replace(
    /task:\{([0-9a-f-]{8,})$/gim,
    (partial, prefix: string) => {
      const matches = citations.filter((cite) => (cite.type === "task" || cite.task_id)
        && (cite.task_id ?? cite.id).toLowerCase().startsWith(prefix.toLowerCase()));
      return matches.length === 1
        ? `task:{${matches[0].task_id ?? matches[0].id}}`
        : partial;
    },
  );
  const byTag = new Map(citations.map((c) => [c.tag, c]));
  const bySourceId = new Map(citations.map((c) => [c.id, c]));
  const byTaskId = new Map<string, ChatCitation>();
  for (const c of citations) {
    if (c.type === "task") byTaskId.set(c.id, c);
    if (c.task_id) byTaskId.set(c.task_id, c);
  }

  // Every widget rendered as a card in this message. Resolve each to the task
  // id / citation tag it cards, tolerating a tag passed where an id was asked.
  const cardedTaskIds = new Set<string>();
  const cardedTags = new Set<string>();
  for (const m of displayContent.matchAll(BLOCK_WIDGET_G)) {
    const kind = m[1] as WidgetKind;
    const key = widgetKey(m[2]);
    if (!isWidgetRef(kind, key)) continue;
    if (kind === "task") {
      const cite = byTag.get(key);
      cardedTaskIds.add(cite ? (cite.task_id ?? cite.id) : key);
      if (cite) cardedTags.add(cite.tag); // also drop the [T#] chip for it
    } else {
      cardedTags.add((byTag.get(key) ?? bySourceId.get(key))?.tag ?? key);
    }
  }

  // An item shown as a card widget already displays its title, so drop any
  // standalone line that just repeats it.
  const cardedTitles = new Set<string>();
  for (const c of citations) {
    const target = c.task_id ?? (c.type === "task" ? c.id : null);
    const carded = cardedTags.has(c.tag) || (target != null && cardedTaskIds.has(target));
    if (carded && c.title) cardedTitles.add(normalizeTitle(c.title));
  }

  const ctx: Ctx = {
    byTag,
    bySourceId,
    byTaskId,
    cardedTitles,
    onOpenTask,
    onShowContent,
  };

  const lines = displayContent.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  // Link previews are pulled onto their own block after the text that mentions
  // them. Deduped per message, and skipped while the answer is still streaming
  // so half-typed URLs aren't fetched.
  const previewed = new Set<string>();
  const pushPreviews = (text: string) => {
    if (caret) return;
    for (const u of collectPreviewUrls(text)) {
      if (previewed.has(u)) continue;
      previewed.add(u);
      blocks.push(<LinkPreviewCard key={`lp-${u}`} url={u} />);
    }
  };
  let key = 0;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const hasWidget = BLOCK_WIDGET.test(line) && hasBlockWidget(line);
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
      while (i < lines.length && listMarker.test(lines[i]) && !hasBlockWidget(lines[i]))
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
      pushPreviews(items.join("\n"));
      continue;
    }
    if (hasWidget) {
      // Drop any leading bullet marker; the card stands on its own.
      blocks.push(...renderWidgetLine(line.replace(/^\s*[-*]\s+/, ""), `wl${key++}`, ctx));
      pushPreviews(line);
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
    pushPreviews(line);
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
