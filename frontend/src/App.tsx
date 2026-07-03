import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Composer } from "@/components/Composer";
import { InboxPanel } from "@/components/inbox/InboxPanel";
import { RunsPanel } from "@/components/runs/RunsPanel";
import { isRunActionable } from "@/components/runs/runLabels";
import { TasksPanel } from "@/components/tasks/TasksPanel";
import { Topbar } from "@/components/Topbar";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Toaster } from "@/components/ui/sonner";
import { useAppData } from "@/hooks/useAppData";
import { useRuns } from "@/hooks/useRuns";
import { api } from "@/lib/api";
import { clearDeepLink, parseDeepLink, pushDeepLink } from "@/lib/deepLinks";
import { useThemePreference } from "@/lib/theme";

export function App() {
  const { tasks, inputs, loading, refresh, loadMoreInputs, hasMoreInputs } = useAppData();
  const { theme, setTheme } = useThemePreference();
  const [tab, setTab] = useState<"tasks" | "runs">("tasks");
  const [mailOpen, setMailOpen] = useState(false);
  const [mailTab, setMailTab] = useState<"inbox" | "runs">("inbox");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const activeRuns = useRuns(!mailOpen && tab === "runs", "active");
  const allRuns = useRuns(mailOpen && mailTab === "runs", "all", false);
  const [unreadInbox, setUnreadInbox] = useState(0);
  const [taskTabUnseen, setTaskTabUnseen] = useState(false);
  const [runTabUnseen, setRunTabUnseen] = useState(false);
  const [unseenTaskIds, setUnseenTaskIds] = useState<Set<string>>(() => new Set());
  const [unseenInputIds, setUnseenInputIds] = useState<Set<string>>(() => new Set());
  const knownTaskIdsRef = useRef<Set<string> | null>(null);
  const knownInputIdsRef = useRef<Set<string> | null>(null);
  const knownActionableRunIdsRef = useRef<Set<number> | null>(null);
  const newestInputReceivedAtRef = useRef<number | null>(null);
  const pendingClearTaskIdsRef = useRef(new Set<string>());
  const pendingClearInputIdsRef = useRef(new Set<string>());
  const tasksActive = !mailOpen && tab === "tasks";
  const runsActive = !mailOpen && tab === "runs";
  const inboxActive = mailOpen && mailTab === "inbox";
  const actionableRunIds = useMemo(
    () => activeRuns.tasks.filter(isRunActionable).map((task) => task.id),
    [activeRuns.tasks],
  );

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
      setSelectedRunId(null);
      return;
    }
    if (link.kind === "task") {
      setSelectedTaskId(link.id);
      setSelectedRunId(null);
      setTab("tasks");
      setMailOpen(false);
      return;
    }
    setSelectedRunId(link.id);
    setSelectedTaskId(null);
    setTab("runs");
    setMailOpen(false);
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

  useEffect(() => {
    loadInboxUnread();
  }, [inputs, loadInboxUnread]);

  useEffect(() => {
    if (mailOpen && mailTab === "inbox" && unreadInbox > 0) markInboxViewed();
  }, [mailOpen, mailTab, markInboxViewed, unreadInbox]);

  useEffect(() => {
    if (!mailOpen || mailTab !== "inbox" || document.visibilityState !== "visible") {
      return;
    }
    markInboxViewed();
  }, [inputs, mailOpen, mailTab, markInboxViewed]);

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

    if (previousIds && !tasksActive) {
      const arrived = tasks.filter((task) => !previousIds.has(task.id));
      if (arrived.length > 0) {
        setTaskTabUnseen(true);
        setUnseenTaskIds((prev) => addAll(prev, arrived.map((task) => task.id)));
      }
    }

    knownTaskIdsRef.current = currentIds;
  }, [loading, tasks, tasksActive]);

  useEffect(() => {
    if (loading) return;

    const currentIds = new Set(inputs.map((input) => input.id));
    const newestReceivedAt = newestTimestamp(inputs.map((input) => input.received_at));
    const previousIds = knownInputIdsRef.current;
    const previousNewest = newestInputReceivedAtRef.current;

    if (previousIds && !inboxActive) {
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
  }, [inboxActive, inputs, loading]);

  useEffect(() => {
    if (activeRuns.loading) return;

    const currentIds = new Set(actionableRunIds);
    const previousIds = knownActionableRunIdsRef.current;

    if (previousIds && !runsActive && actionableRunIds.some((id) => !previousIds.has(id))) {
      setRunTabUnseen(true);
    }

    knownActionableRunIdsRef.current = currentIds;
  }, [actionableRunIds, activeRuns.loading, runsActive]);

  useEffect(() => {
    if (tasksActive) setTaskTabUnseen(false);
  }, [tasksActive]);

  useEffect(() => {
    if (runsActive) setRunTabUnseen(false);
  }, [runsActive]);

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
    const pending = pendingClearTaskIdsRef.current;
    if (pending.size === 0) return;

    setUnseenTaskIds((prev) => removeAll(prev, pending));
    pendingClearTaskIdsRef.current = new Set();
  }, [tasksActive]);

  useEffect(() => {
    if (inboxActive) return;
    const pending = pendingClearInputIdsRef.current;
    if (pending.size === 0) return;

    setUnseenInputIds((prev) => removeAll(prev, pending));
    pendingClearInputIdsRef.current = new Set();
  }, [inboxActive]);

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

  const onTabChange = useCallback(
    (value: string) => {
      if (value === "tasks" || value === "runs") setTab(value);
    },
    [],
  );

  const onMailTabChange = useCallback((value: string) => {
    if (value === "inbox" || value === "runs") setMailTab(value);
  }, []);

  const openTask = useCallback((id: string) => {
    pushDeepLink({ kind: "task", id });
    setSelectedTaskId(id);
    setSelectedRunId(null);
    setTab("tasks");
    setMailOpen(false);
  }, []);

  const openRun = useCallback(
    (id: number) => {
      pushDeepLink({ kind: "run", id });
      setSelectedRunId(id);
      setSelectedTaskId(null);
      setTab("runs");
    },
    [],
  );

  const closeSelectedModal = useCallback(() => {
    clearDeepLink();
    setSelectedTaskId(null);
    setSelectedRunId(null);
  }, []);

  return (
    <div className="min-h-dvh pb-[120px]">
      <Topbar
        theme={theme}
        onThemeChange={setTheme}
        mode={mailOpen ? "mail" : "normal"}
        unreadInbox={unreadInbox}
        onMailOpen={() => {
          setMailTab("inbox");
          setMailOpen(true);
        }}
        onBack={() => setMailOpen(false)}
      />
      <main className="mx-auto max-w-2xl px-4 py-4">
        {mailOpen ? (
          <Tabs value={mailTab} onValueChange={onMailTabChange}>
            <TabsList className="mb-4 grid w-full grid-cols-2">
              <TabsTrigger value="inbox">Inbox</TabsTrigger>
              <TabsTrigger value="runs">Runs</TabsTrigger>
            </TabsList>
            <TabsContent value="inbox">
              <InboxPanel
                inputs={inputs}
                onChanged={refresh}
                onLoadMore={loadMoreInputs}
                hasMore={hasMoreInputs}
                unseenInputIds={unseenInputIds}
                onInputsVisible={markInputsVisible}
              />
            </TabsContent>
            <TabsContent value="runs">
              <RunsPanel
                {...allRuns}
                selectedRunId={selectedRunId}
                onRunOpen={openRun}
                onSelectedRunClose={closeSelectedModal}
              />
            </TabsContent>
          </Tabs>
        ) : (
          <Tabs value={tab} onValueChange={onTabChange}>
            <TabsList className="mb-4 grid w-full grid-cols-2">
              <TabsTrigger value="tasks">
                {taskTabUnseen && <UnseenDot />}
                Tasks
                {tasks.length > 0 && (
                  <span className="ml-1.5 text-xs text-muted-foreground">
                    {tasks.length}
                  </span>
                )}
              </TabsTrigger>
              <TabsTrigger value="runs">
                {runTabUnseen && <UnseenDot />}
                Runs
                {activeRuns.tasks.length > 0 && (
                  <span className="ml-1.5 text-xs text-muted-foreground">
                    {activeRuns.tasks.length}
                  </span>
                )}
              </TabsTrigger>
            </TabsList>
            <TabsContent value="tasks">
              <TasksPanel
                tasks={tasks}
                onChanged={refresh}
                selectedTaskId={selectedTaskId}
                onTaskOpen={openTask}
                onSelectedTaskClose={closeSelectedModal}
                unseenTaskIds={unseenTaskIds}
                onTaskVisible={markTaskVisible}
              />
            </TabsContent>
            <TabsContent value="runs">
              <RunsPanel
                {...activeRuns}
                selectedRunId={selectedRunId}
                onRunOpen={openRun}
                onSelectedRunClose={closeSelectedModal}
              />
            </TabsContent>
          </Tabs>
        )}
      </main>
      <Composer onCreated={refresh} />
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

function UnseenDot() {
  return (
    <span
      aria-label="Unread"
      title="Unread"
      className="mr-1.5 inline-block h-2 w-2 shrink-0 rounded-full bg-emerald-500"
    />
  );
}
