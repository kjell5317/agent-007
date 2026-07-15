import {
  Children,
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useState,
} from "react";
import type { ComponentType, MouseEvent, ReactNode } from "react";
import {
  BookOpen,
  CalendarDays,
  Cake,
  Check,
  Copy,
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
//   • a notion.so link → a Notion page chip
//   • any other http(s) link → a fetched preview card (title/description)

interface Rule {
  re: RegExp;
  render: (m: RegExpExecArray, key: string, ctx: Ctx) => ReactNode;
}

interface Ctx {
  byTag: Map<string, ChatCitation>;
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
              // Ignore keys bubbling from inner controls (contact links/copy).
              if (e.target !== e.currentTarget) return;
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                activate();
              }
            }
          : undefined
      }
      className={cn(
        "flex max-w-full items-center gap-3 rounded-xl border bg-card px-3 py-2.5 text-left shadow-sm transition-colors",
        clickable && "cursor-pointer hover:border-primary/40 hover:bg-accent",
      )}
    >
      <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
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

// A contact detail (email/phone/address): the value is a click-to-act link
// (mailto: / tel: / maps) plus a copy-to-clipboard button. Both stop
// propagation so they don't also trigger the surrounding card's open action.
function ContactDetail({
  Icon,
  value,
  href,
  external,
}: {
  Icon: ComponentType<{ className?: string }>;
  value: string;
  href: string;
  external?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const onCopy = (e: MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    void navigator.clipboard
      ?.writeText(value)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => {});
  };
  return (
    <div className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
      <Icon className="h-3 w-3 shrink-0" />
      <a
        href={href}
        onClick={(e) => e.stopPropagation()}
        target={external ? "_blank" : undefined}
        rel={external ? "noopener noreferrer" : undefined}
        className="min-w-0 flex-1 truncate text-primary underline underline-offset-2"
      >
        {value}
      </a>
      <button
        type="button"
        onClick={onCopy}
        title={copied ? "Copied" : "Copy"}
        aria-label={`Copy ${value}`}
        className="shrink-0 rounded p-0.5 text-muted-foreground/70 transition-colors hover:bg-accent hover:text-foreground"
      >
        {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
      </button>
    </div>
  );
}

function ContactCard({ cite }: { cite: ChatCitation }) {
  const meta = citeMeta(cite);
  const emails = meta.emails ?? [];
  const phones = meta.phones ?? [];
  const addresses = meta.addresses ?? [];
  return (
    <WidgetShell Icon={UserRound} title={cite.title || "Contact"} href={cite.url}>
      {meta.org && <div className="mt-0.5 truncate text-xs text-muted-foreground">{meta.org}</div>}
      {emails.map((e, i) => (
        <ContactDetail key={`e${i}`} Icon={Mail} value={e} href={`mailto:${e}`} />
      ))}
      {phones.map((p, i) => (
        <ContactDetail key={`p${i}`} Icon={Phone} value={p} href={`tel:${p.replace(/[^+\d]/g, "")}`} />
      ))}
      {meta.birthday && <DetailRow Icon={Cake}>{meta.birthday}</DetailRow>}
      {addresses.map((a, i) => (
        <ContactDetail key={`a${i}`} Icon={MapPin} value={a} href={mapsUrl(a)} external />
      ))}
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

// A fetched preview card for a plain http(s) link, pulled to its own block
// below the text (WhatsApp-style). While loading: a skeleton; if the URL can't
// be previewed: nothing (the inline link in the text still stands on its own).
function LinkPreviewCard({ url }: { url: string }) {
  const [preview, setPreview] = useState<LinkPreview | null>(null);
  const [done, setDone] = useState(false);
  const [imgFailed, setImgFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setPreview(null);
    setDone(false);
    setImgFailed(false);
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
    return <div className="my-1.5 h-16 animate-pulse rounded-xl border bg-muted/40" />;
  }
  if (!preview) return null;

  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="my-1.5 flex max-w-full items-stretch gap-3 overflow-hidden rounded-xl border bg-card text-left shadow-sm transition-colors hover:border-primary/40 hover:bg-accent"
    >
      {preview.image && !imgFailed && (
        <img
          src={preview.image}
          alt=""
          loading="lazy"
          onError={() => setImgFailed(true)}
          className="w-16 shrink-0 self-stretch object-cover"
        />
      )}
      <div className="min-w-0 flex-1 px-3 py-2">
        <div className="truncate text-sm font-medium">{preview.title}</div>
        {preview.description && (
          <div className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
            {preview.description}
          </div>
        )}
        <div className="mt-0.5 flex items-center gap-1 text-[11px] text-muted-foreground">
          <ExternalLink className="h-3 w-3 shrink-0" />
          <span className="truncate">{preview.site_name || safeHost(url)}</span>
        </div>
      </div>
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
    byTaskId,
    cardedTitles,
    onOpenTask,
    onShowContent,
  };

  const lines = content.replace(/\r\n/g, "\n").split("\n");
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
