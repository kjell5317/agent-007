import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Plus } from "lucide-react";
import { Composer } from "@/components/Composer";
import { InboxPanel } from "@/components/inbox/InboxPanel";
import { PointsPanel } from "@/components/points/PointsPanel";
import { ChatComposer } from "@/components/search/ChatComposer";
import { ChatPanel } from "@/components/search/ChatPanel";
import { TaskDetailModal } from "@/components/tasks/TaskDetailModal";
import { TasksPanel } from "@/components/tasks/TasksPanel";
import { Topbar } from "@/components/Topbar";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Toaster } from "@/components/ui/sonner";
import { useAppData } from "@/hooks/useAppData";
import { useRuns } from "@/hooks/useRuns";
import { useSearchChat } from "@/hooks/useSearchChat";
import { api } from "@/lib/api";
import { clearDeepLink, parseDeepLink, pushDeepLink } from "@/lib/deepLinks";
import type { KotxTask } from "@/lib/kotx";
import { useThemePreference } from "@/lib/theme";
import type { Task } from "@/lib/types";

export function App() {
  const { tasks, inputs, loading, refresh, loadMoreInputs, hasMoreInputs } = useAppData();
  const { theme, setTheme } = useThemePreference();
  // "tasks" / "chat" are the two tabs of the main view; "mail" / "points" are
  // overlays reached from the topbar (Back returns to the last active tab).
  const [view, setView] = useState<"tasks" | "chat" | "mail" | "points">(
    "tasks",
  );
  const lastTabRef = useRef<"tasks" | "chat">("tasks");
  const chat = useSearchChat();
  const mailOpen = view === "mail";
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  // A #run/<kotxId> deep link (legacy runs modal) waiting for the task list to
  // load so it can resolve to the adopting task.
  const [pendingRunId, setPendingRunId] = useState<number | null>(null);
  const runs = useRuns(!mailOpen, "all");
  const [unreadInbox, setUnreadInbox] = useState(0);
  const [unseenTaskIds, setUnseenTaskIds] = useState<Set<string>>(() => new Set());
  const [unseenInputIds, setUnseenInputIds] = useState<Set<string>>(() => new Set());
  const knownTaskIdsRef = useRef<Set<string> | null>(null);
  const knownInputIdsRef = useRef<Set<string> | null>(null);
  const newestInputReceivedAtRef = useRef<number | null>(null);
  const pendingClearTaskIdsRef = useRef(new Set<string>());
  const pendingClearInputIdsRef = useRef(new Set<string>());
  const tasksActive = view === "tasks";
  const inboxActive = view === "mail";

  const selectTab = useCallback((next: string) => {
    const tab = next === "chat" ? "chat" : "tasks";
    lastTabRef.current = tab;
    setView(tab);
  }, []);

  // Back / Escape out of the mail or points overlay, returning to whichever tab
  // was last active.
  const leaveOverlay = useCallback(() => {
    setView(lastTabRef.current);
  }, []);

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
    if (mailOpen && unreadInbox > 0) markInboxViewed();
  }, [mailOpen, markInboxViewed, unreadInbox]);

  useEffect(() => {
    if (!mailOpen || document.visibilityState !== "visible") return;
    markInboxViewed();
  }, [inputs, mailOpen, markInboxViewed]);

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
        if (input.source === "manual" || previousIds.has(input.id)) return false;
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
    pushDeepLink({ kind: "task", id });
    setSelectedTaskId(id);
  }, []);

  const closeSelectedModal = useCallback(() => {
    clearDeepLink();
    setSelectedTaskId(null);
  }, []);

  const [fetchedTask, setFetchedTask] = useState<Task | null>(null);
  const selectedListTask = useMemo(
    () => tasks.find((task) => task.id === selectedTaskId) ?? null,
    [selectedTaskId, tasks],
  );
  const selectedTask = selectedTaskId ? selectedListTask ?? fetchedTask : null;

  useEffect(() => {
    if (!selectedTaskId || selectedListTask) {
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

  return (
    <div className="min-h-dvh pb-24">
      <Topbar
        theme={theme}
        onThemeChange={setTheme}
        mode={view === "mail" || view === "points" ? view : "normal"}
        unreadInbox={unreadInbox}
        onMailOpen={() => setView("mail")}
        onPointsOpen={() => setView("points")}
        onBack={leaveOverlay}
      />
      <main className="mx-auto max-w-2xl px-4 py-4">
        {view === "mail" ? (
          <InboxPanel
            inputs={inputs}
            onChanged={refresh}
            onLoadMore={loadMoreInputs}
            hasMore={hasMoreInputs}
            unseenInputIds={unseenInputIds}
            onInputsVisible={markInputsVisible}
            onOpenTask={openTask}
          />
        ) : view === "points" ? (
          <PointsPanel onOpenTask={openTask} />
        ) : (
          <Tabs value={view} onValueChange={selectTab}>
            <div className="mb-4 flex items-center gap-2">
              <TabsList className="grid h-10 flex-1 grid-cols-2">
                <TabsTrigger value="tasks">
                  Tasks
                  {tasks.length > 0 && (
                    <span className="ml-1.5 text-xs text-muted-foreground">
                      {tasks.length}
                    </span>
                  )}
                </TabsTrigger>
                <TabsTrigger value="chat">Chat</TabsTrigger>
              </TabsList>
              {view === "chat" && chat.messages.length > 0 && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => chat.newChat()}
                  className="shrink-0 gap-1.5"
                >
                  <Plus className="h-4 w-4" />
                  New chat
                </Button>
              )}
            </div>
            <TabsContent value="tasks">
              <TasksPanel
                tasks={tasks}
                kotxTasks={kotxTasks}
                onChanged={refresh}
                onKotxChanged={runs.refresh}
                onTaskOpen={openTask}
                unseenTaskIds={unseenTaskIds}
                onTaskVisible={markTaskVisible}
              />
            </TabsContent>
            <TabsContent value="chat">
              <ChatPanel
                messages={chat.messages}
                streaming={chat.streaming}
                onOpenTask={openTask}
                recent={chat.recent}
                onLoadChat={chat.loadChat}
              />
            </TabsContent>
          </Tabs>
        )}
      </main>
      {selectedTask && (
        <TaskDetailModal
          task={selectedTask}
          kotxTask={selectedKotxTask}
          onClose={closeSelectedModal}
          onChanged={refresh}
          onKotxChanged={runs.refresh}
        />
      )}
      {view === "chat" ? (
        <ChatComposer
          onSend={chat.send}
          streaming={chat.streaming}
          onClose={() => selectTab("tasks")}
          onOpenTask={openTask}
        />
      ) : view === "tasks" ? (
        <Composer onCreated={refresh} onOpenTask={openTask} />
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
