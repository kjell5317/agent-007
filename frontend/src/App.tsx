import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Composer } from "@/components/Composer";
import { InboxPanel } from "@/components/inbox/InboxPanel";
import { LabelsPanel } from "@/components/labels/LabelsPanel";
import { NotesPanel } from "@/components/notes/NotesPanel";
import { PointsPanel } from "@/components/points/PointsPanel";
import { ChatComposer } from "@/components/search/ChatComposer";
import { ChatPanel } from "@/components/search/ChatPanel";
import { PopularSearchResults } from "@/components/search/PopularSearchResults";
import { EMPTY_SEARCH_FILTERS, SearchFilters, type SearchFiltersState } from "@/components/search/SearchFilters";
import { SearchResults } from "@/components/search/SearchResults";
import { TaskDetailModal } from "@/components/tasks/TaskDetailModal";
import { TaskCard } from "@/components/tasks/TaskCard";
import { TasksPanel } from "@/components/tasks/TasksPanel";
import { Topbar } from "@/components/Topbar";
import { Toaster } from "@/components/ui/sonner";
import { useAppData } from "@/hooks/useAppData";
import { useRuns } from "@/hooks/useRuns";
import { useSearchChat } from "@/hooks/useSearchChat";
import { api } from "@/lib/api";
import { getUserTimezone, setUserTimezone } from "@/lib/dates";
import { inputTitle, senderName } from "@/lib/inbox";
import { clearDeepLink, parseDeepLink, pushDeepLink, replaceDeepLink } from "@/lib/deepLinks";
import type { KotxTask } from "@/lib/kotx";
import { useThemePreference } from "@/lib/theme";
import type { RawInput, SearchHit, Task } from "@/lib/types";

export function App() {
  const { tasks, inputs, loading, refresh, loadMoreInputs, hasMoreInputs } = useAppData();
  const [timeZone, setTimeZone] = useState(getUserTimezone);
  useEffect(() => {
    api.getSettings().then(({ user_timezone }) => {
      setUserTimezone(user_timezone);
      setTimeZone(getUserTimezone());
    }).catch(() => {});
  }, []);
  const { theme, setTheme } = useThemePreference();
  const [view, setView] = useState<
    "tasks" | "chat" | "points" | "labels"
  >("tasks");
  const searchCloseTimerRef = useRef<number | null>(null);
  const [searchClosing, setSearchClosing] = useState(false);
  const chat = useSearchChat();
  const [searchQuery, setSearchQuery] = useState("");
  const [searchSubmitted, setSearchSubmitted] = useState(false);
  const [searchFilters, setSearchFilters] = useState<SearchFiltersState>(EMPTY_SEARCH_FILTERS);
  const inboxOpen = view === "chat" && !searchQuery.trim() && searchFilters.kind === "messages";
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const taskNavigationRequestRef = useRef(0);
  // A #run/<kotxId> deep link (legacy runs modal) waiting for the task list to
  // load so it can resolve to the adopting task.
  const [pendingRunId, setPendingRunId] = useState<number | null>(null);
  const runs = useRuns(!inboxOpen, "all");
  const [unreadInbox, setUnreadInbox] = useState(0);
  const [unseenTaskIds, setUnseenTaskIds] = useState<Set<string>>(() => new Set());
  const [unseenInputIds, setUnseenInputIds] = useState<Set<string>>(() => new Set());
  const knownTaskIdsRef = useRef<Set<string> | null>(null);
  const knownInputIdsRef = useRef<Set<string> | null>(null);
  const newestInputReceivedAtRef = useRef<number | null>(null);
  const pendingClearTaskIdsRef = useRef(new Set<string>());
  const pendingClearInputIdsRef = useRef(new Set<string>());
  const tasksActive = view === "tasks" || (view === "chat" && !searchQuery.trim() && searchFilters.kind === "tasks");
  const inboxActive = inboxOpen;

  const closeSearchTo = useCallback(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setView("tasks");
      return;
    }
    setSearchClosing(true);
    if (searchCloseTimerRef.current !== null) window.clearTimeout(searchCloseTimerRef.current);
    searchCloseTimerRef.current = window.setTimeout(() => {
      setView("tasks");
      setSearchClosing(false);
      searchCloseTimerRef.current = null;
    }, 220);
  }, []);

  useEffect(() => () => {
    if (searchCloseTimerRef.current !== null) window.clearTimeout(searchCloseTimerRef.current);
  }, []);

  const openChat = useCallback(() => {
    chat.newChat();
    setSearchQuery("");
    setSearchSubmitted(false);
    setSearchFilters(EMPTY_SEARCH_FILTERS);
    setView("chat");
  }, [chat]);

  const changeSearchQuery = (next: string) => {
    if (next !== searchQuery) setSearchSubmitted(false);
    setSearchQuery(next);
    if (!next.trim() && (searchFilters.kind === "contacts" || searchFilters.kind === "events" || searchFilters.kind === "files")) {
      setSearchFilters(EMPTY_SEARCH_FILTERS);
    }
  };

  const runAiSearch = (query: string) => {
    chat.send(query, searchFilters.kind ? {
      kind: searchFilters.kind,
      label: searchFilters.label || undefined,
      source: searchFilters.source || undefined,
      format: searchFilters.format || undefined,
    } : undefined);
    setSearchQuery("");
    setSearchFilters(EMPTY_SEARCH_FILTERS);
    setSearchSubmitted(false);
  };

  const leaveOverlay = useCallback(() => {
    taskNavigationRequestRef.current += 1;
    if (selectedTaskId) {
      clearDeepLink();
      setSelectedTaskId(null);
    }
    if (view === "chat") closeSearchTo();
    else setView("tasks");
  }, [closeSearchTo, selectedTaskId, view]);

  const clearPendingTaskIds = useCallback(() => {
    const pending = pendingClearTaskIdsRef.current;
    if (pending.size === 0) return;

    setUnseenTaskIds((prev) => removeAll(prev, pending));
    pendingClearTaskIdsRef.current = new Set();
  }, []);

  const clearPendingInputIds = useCallback(() => {
    const pending = pendingClearInputIdsRef.current;
    if (pending.size === 0) return;

    setUnseenInputIds((prev) => removeAll(prev, pending));
    pendingClearInputIdsRef.current = new Set();
  }, []);

  const kotxTasks = useMemo(() => {
    const map = new Map<number, KotxTask>();
    for (const run of runs.tasks) map.set(run.id, run);
    return map;
  }, [runs.tasks]);

  const loadInboxUnread = useCallback(async () => {
    try {
      const inboxRes = await api.unreadInputCount();
      setUnreadInbox(inboxRes.count);
    } catch {
      setUnreadInbox(0);
    }
  }, []);

  const markInboxViewed = useCallback(async () => {
    setUnreadInbox(0);
    try {
      const res = await api.markInputsSeen();
      setUnreadInbox(res.count);
    } catch {
      loadInboxUnread();
    }
  }, [loadInboxUnread]);

  useEffect(() => {
    loadInboxUnread();
  }, [loadInboxUnread]);

  const applyLocation = useCallback(() => {
    taskNavigationRequestRef.current += 1;
    const link = parseDeepLink();
    if (!link) {
      setSelectedTaskId(null);
      setPendingRunId(null);
      return;
    }
    if (link.kind === "task") {
      setSelectedTaskId(link.id);
      setPendingRunId(null);
      setView("tasks");
      return;
    }
    setPendingRunId(link.id);
    setSelectedTaskId(null);
    setView("tasks");
  }, []);

  useEffect(() => {
    applyLocation();
    window.addEventListener("hashchange", applyLocation);
    window.addEventListener("popstate", applyLocation);
    return () => {
      window.removeEventListener("hashchange", applyLocation);
      window.removeEventListener("popstate", applyLocation);
    };
  }, [applyLocation]);

  // Resolve legacy #run/<kotxId> links to the adopting task once tasks load.
  useEffect(() => {
    if (pendingRunId === null || loading) return;
    const match = tasks.find((task) => task.kotx_task_id === pendingRunId);
    setPendingRunId(null);
    if (match) {
      pushDeepLink({ kind: "task", id: match.id });
      setSelectedTaskId(match.id);
    } else {
      clearDeepLink();
    }
  }, [loading, pendingRunId, tasks]);

  useEffect(() => {
    loadInboxUnread();
  }, [inputs, loadInboxUnread]);

  useEffect(() => {
    if (inboxOpen && unreadInbox > 0) markInboxViewed();
  }, [inboxOpen, markInboxViewed, unreadInbox]);

  useEffect(() => {
    if (!inboxOpen || document.visibilityState !== "visible") return;
    markInboxViewed();
  }, [inputs, inboxOpen, markInboxViewed]);

  // Refresh the unread badge when the app comes back to the foreground. The
  // input list itself is already refreshed by useAppData on visibilitychange /
  // focus; the badge is owned here, so it needs its own listener.
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === "visible") loadInboxUnread();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", loadInboxUnread);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", loadInboxUnread);
    };
  }, [loadInboxUnread]);

  useEffect(() => {
    if (loading) return;

    const currentIds = new Set(tasks.map((task) => task.id));
    const previousIds = knownTaskIdsRef.current;

    if (previousIds) {
      const arrived = tasks.filter((task) => !previousIds.has(task.id));
      if (arrived.length > 0) {
        setUnseenTaskIds((prev) => addAll(prev, arrived.map((task) => task.id)));
      }
    }

    knownTaskIdsRef.current = currentIds;
  }, [loading, tasks]);

  useEffect(() => {
    if (loading) return;

    const currentIds = new Set(inputs.map((input) => input.id));
    const newestReceivedAt = newestTimestamp(inputs.map((input) => input.received_at));
    const previousIds = knownInputIdsRef.current;
    const previousNewest = newestInputReceivedAtRef.current;

    if (previousIds) {
      const arrived = inputs.filter((input) => {
        if (input.source === "manual" || input.source === "web_research" || previousIds.has(input.id)) return false;
        const receivedAt = Date.parse(input.received_at);
        return (
          !Number.isNaN(receivedAt) &&
          (previousNewest == null || receivedAt > previousNewest)
        );
      });
      if (arrived.length > 0) {
        setUnseenInputIds((prev) => addAll(prev, arrived.map((input) => input.id)));
      }
    }

    knownInputIdsRef.current = currentIds;
    newestInputReceivedAtRef.current = newestReceivedAt;
  }, [inputs, loading]);

  useEffect(() => {
    const currentIds = new Set(tasks.map((task) => task.id));
    setUnseenTaskIds((prev) => intersect(prev, currentIds));
    pendingClearTaskIdsRef.current = intersect(
      pendingClearTaskIdsRef.current,
      currentIds,
    );
  }, [tasks]);

  useEffect(() => {
    const currentIds = new Set(inputs.map((input) => input.id));
    setUnseenInputIds((prev) => intersect(prev, currentIds));
    pendingClearInputIdsRef.current = intersect(
      pendingClearInputIdsRef.current,
      currentIds,
    );
  }, [inputs]);

  useEffect(() => {
    if (tasksActive) return;
    clearPendingTaskIds();
  }, [clearPendingTaskIds, tasksActive]);

  useEffect(() => {
    if (inboxActive) return;
    clearPendingInputIds();
  }, [clearPendingInputIds, inboxActive]);

  useEffect(() => {
    const clearPendingVisibleIds = () => {
      clearPendingTaskIds();
      clearPendingInputIds();
    };
    const onVisibility = () => {
      if (document.visibilityState !== "visible") clearPendingVisibleIds();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("blur", clearPendingVisibleIds);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("blur", clearPendingVisibleIds);
    };
  }, [clearPendingInputIds, clearPendingTaskIds]);

  const markTaskVisible = useCallback(
    (id: string) => {
      if (!tasksActive || !unseenTaskIds.has(id)) return;
      pendingClearTaskIdsRef.current.add(id);
    },
    [tasksActive, unseenTaskIds],
  );

  const markInputsVisible = useCallback(
    (ids: string[]) => {
      if (!inboxActive) return;
      for (const id of ids) {
        if (unseenInputIds.has(id)) pendingClearInputIdsRef.current.add(id);
      }
    },
    [inboxActive, unseenInputIds],
  );

  // Opening a task keeps the current view — from the inbox the modal shows on
  // top of it, so closing lands back where the click happened.
  const openTask = useCallback((id: string) => {
    if (selectedTaskId === id) return;
    const requestId = ++taskNavigationRequestRef.current;
    const listedTask = tasks.find((candidate) => candidate.id === id);
    const finish = (loadedTask?: Task) => {
      if (requestId !== taskNavigationRequestRef.current) return;
      const openedTask = loadedTask ?? listedTask;
      if (openedTask) void api.recordSearchClick(taskSearchHit(openedTask)).catch(() => {});
      if (selectedTaskId) replaceDeepLink({ kind: "task", id });
      else pushDeepLink({ kind: "task", id });
      if (loadedTask) setFetchedTask(loadedTask);
      setSelectedTaskId(id);
    };
    if (listedTask) finish();
    else void api.getTask(id).then(finish).catch((error) => {
      if (requestId === taskNavigationRequestRef.current) toast.error((error as Error).message);
    });
  }, [selectedTaskId, tasks]);

  const recordInputClick = useCallback((input: RawInput) => {
    void api.recordSearchClick(inputSearchHit(input)).catch(() => {});
  }, []);

  const selectedTaskSnapshot = useRef<Task | null>(null);
  const [fetchedTask, setFetchedTask] = useState<Task | null>(null);
  const closeSelectedModal = useCallback(() => {
    taskNavigationRequestRef.current += 1;
    clearDeepLink();
    selectedTaskSnapshot.current = null;
    setFetchedTask(null);
    setSelectedTaskId(null);
  }, []);

  const selectedListTask = useMemo(
    () => tasks.find((task) => task.id === selectedTaskId) ?? null,
    [selectedTaskId, tasks],
  );
  const selectedTask = selectedTaskId
    ? selectedListTask ??
      (fetchedTask?.id === selectedTaskId ? fetchedTask : null) ??
      selectedTaskSnapshot.current
    : null;
  if (selectedTask) selectedTaskSnapshot.current = selectedTask;

  useEffect(() => {
    if (!selectedTaskId || selectedListTask || selectedTaskSnapshot.current?.id === selectedTaskId) {
      setFetchedTask(null);
      return;
    }
    let cancelled = false;
    api
      .getTask(selectedTaskId)
      .then((task) => {
        if (!cancelled) setFetchedTask(task);
      })
      .catch(() => {
        if (!cancelled) closeSelectedModal();
      });
    return () => {
      cancelled = true;
    };
  }, [closeSelectedModal, selectedListTask, selectedTaskId]);

  const selectedKotxTask =
    selectedTask && selectedTask.kotx_task_id != null
      ? kotxTasks.get(selectedTask.kotx_task_id) ?? null
      : null;

  const renderTasks = (label = "") => (
    <TasksPanel
      timeZone={timeZone}
      tasks={label ? tasks.filter((task) => task.label?.toLowerCase() === label.toLowerCase()) : tasks}
      kotxTasks={kotxTasks}
      onChanged={refresh}
      onKotxChanged={runs.refresh}
      onTaskOpen={openTask}
      unseenTaskIds={unseenTaskIds}
      onTaskVisible={markTaskVisible}
    />
  );

  const renderInbox = (source = "") => (
    <InboxPanel
      inputs={source ? inputs.filter((input) => input.source === source) : inputs}
      onChanged={refresh}
      onLoadMore={loadMoreInputs}
      hasMore={hasMoreInputs}
      unseenInputIds={unseenInputIds}
      onInputsVisible={markInputsVisible}
      onOpenTask={openTask}
      onActivate={recordInputClick}
    />
  );

  const renderFlatTasks = (label: string) => {
    const filtered = tasks.filter((task) => !task.is_container &&
      (!label || task.label?.toLowerCase() === label.toLowerCase()));
    return filtered.length ? (
      <div className="space-y-2">
        {filtered.map((task) => (
          <TaskCard
            key={task.id}
            task={task}
            kotxTask={task.kotx_task_id != null ? kotxTasks.get(task.kotx_task_id) ?? null : null}
            onChanged={refresh}
            onKotxChanged={runs.refresh}
            onOpen={openTask}
            unseen={unseenTaskIds.has(task.id)}
            onVisible={markTaskVisible}
          />
        ))}
      </div>
    ) : <p className="py-12 text-center text-sm text-muted-foreground">No matching tasks.</p>;
  };

  return (
    <div className={view === "tasks" ? "min-h-dvh pb-28" : "min-h-dvh pb-8"}>
      <Topbar
        theme={theme}
        onThemeChange={setTheme}
        mode={
          view === "chat" || view === "points" || view === "labels"
            ? view
            : "normal"
        }
        title={
          view === "points" ? "Points" :
          view === "labels" ? "Labels" :
          view === "chat" ? "Chat" : "Tasks"
        }
        onChatOpen={openChat}
        chatSearch={view === "chat" ? (
          <ChatComposer
            value={searchQuery}
            onChange={changeSearchQuery}
            submitted={searchSubmitted}
            streaming={chat.streaming}
            onClose={leaveOverlay}
            onEnter={() => setSearchSubmitted(true)}
          />
        ) : undefined}
        onPointsOpen={() => setView("points")}
        onLabelsOpen={() => setView("labels")}
        onBack={leaveOverlay}
      />
      <main className={`mx-auto max-w-2xl px-4 pb-4 pt-2 ${view === "chat" ? searchClosing ? "animate-search-content-close" : "animate-search-content-open" : ""}`}>
        {view === "points" ? (
          <PointsPanel onOpenTask={openTask} />
        ) : view === "labels" ? (
          <LabelsPanel />
        ) : view === "tasks" ? (
          loading ? <div className="flex justify-center py-12" role="status" aria-label="Loading tasks"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div> : renderTasks()
        ) : (
          <div>
            {(searchQuery.trim() || chat.messages.length === 0) && <SearchFilters filters={searchFilters} query={searchQuery} onChange={setSearchFilters} />}
            <div className={searchQuery.trim() || chat.messages.length === 0 ? "pt-3" : undefined}>
            {searchQuery.trim() ? (
              <SearchResults query={searchQuery} filters={searchFilters} tasks={tasks} inputs={inputs} submitted={searchSubmitted} onAiSearch={runAiSearch} onOpenTask={openTask} onChanged={refresh} />
            ) : searchFilters.kind === "tasks" ? (
              loading ? <div className="flex justify-center py-12" role="status" aria-label="Loading tasks"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div> : renderFlatTasks(searchFilters.label)
            ) : searchFilters.kind === "messages" ? (
              loading ? <div className="flex justify-center py-12" role="status" aria-label="Loading messages"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div> : renderInbox(searchFilters.source)
            ) : searchFilters.kind === "notes" ? (
              <NotesPanel key={searchFilters.source} source={searchFilters.source} />
            ) : searchFilters.kind === null && chat.messages.length > 0 ? (
              <ChatPanel
                messages={chat.messages}
                streaming={chat.streaming}
                onOpenTask={openTask}
                recent={chat.recent}
                onLoadChat={chat.loadChat}
              />
            ) : searchFilters.kind === null ? (
              <PopularSearchResults tasks={tasks} inputs={inputs} onOpenTask={openTask} onChanged={refresh} />
            ) : (
              null
            )}
            </div>
          </div>
        )}
      </main>
      {selectedTask && (
        <TaskDetailModal
          key={selectedTask.id}
          task={selectedTask}
          knownTasks={tasks}
          kotxTask={selectedKotxTask}
          onClose={closeSelectedModal}
          onChanged={refresh}
          onOpenTask={openTask}
          onKotxChanged={runs.refresh}
        />
      )}
      {view === "tasks" ? (
        <Composer onCreated={refresh} />
      ) : null}
      <Toaster />
    </div>
  );
}

function newestTimestamp(values: string[]): number | null {
  let newest: number | null = null;
  for (const value of values) {
    const time = Date.parse(value);
    if (Number.isNaN(time)) continue;
    if (newest == null || time > newest) newest = time;
  }
  return newest;
}

function taskSearchHit(task: Task): SearchHit {
  return {
    type: "task", id: task.id, title: task.title, snippet: task.description,
    url: task.link, task_id: task.id, source: null, sender: null,
    status: task.status, ts: task.updated_at, score: 0,
  };
}

function inputSearchHit(input: RawInput): SearchHit {
  return {
    type: "input", id: input.id, title: inputTitle(input), snippet: input.content,
    url: null, task_id: input.task_id, source: input.source,
    sender: senderName(input), status: input.status, ts: input.received_at, score: 0,
  };
}

function addAll<T>(source: ReadonlySet<T>, values: T[]): Set<T> {
  if (values.length === 0) return source instanceof Set ? source : new Set(source);
  const next = new Set(source);
  for (const value of values) next.add(value);
  return next;
}

function removeAll<T>(source: ReadonlySet<T>, values: ReadonlySet<T>): Set<T> {
  if (values.size === 0) return source instanceof Set ? source : new Set(source);
  const next = new Set(source);
  for (const value of values) next.delete(value);
  return next;
}

function intersect<T>(source: ReadonlySet<T>, allowed: ReadonlySet<T>): Set<T> {
  const next = new Set<T>();
  for (const value of source) {
    if (allowed.has(value)) next.add(value);
  }
  return next;
}
