import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Composer } from "@/components/Composer";
import { InboxPanel } from "@/components/inbox/InboxPanel";
import { RunsPanel } from "@/components/runs/RunsPanel";
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
  const { tasks, inputs, refresh, loadMoreInputs, hasMoreInputs } = useAppData();
  const { theme, setTheme } = useThemePreference();
  const [tab, setTab] = useState("tasks");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const runs = useRuns(tab === "runs");
  // Runs awaiting my action (ready to start, or a review ready to post).
  const runsActionable = useMemo(
    () => runs.tasks.filter((t) => t.canStart || t.canApprove).length,
    [runs.tasks],
  );
  const [unreadInbox, setUnreadInbox] = useState(0);
  const [unreadTasks, setUnreadTasks] = useState(0);
  // Page-load snapshots of the per-tab "last seen" watermarks. Used to draw
  // per-card unread dots. We snapshot once at mount and intentionally do NOT
  // update during the session — so the dots persist through tab switches
  // (which reset the server-side watermark) and only clear on the next full
  // page load.
  const [seenInboxAt, setSeenInboxAt] = useState<string | null>(null);
  const [seenTasksAt, setSeenTasksAt] = useState<string | null>(null);
  const snapshotsTaken = useRef(false);

  const loadUnread = useCallback(async () => {
    try {
      const [inboxRes, tasksRes] = await Promise.all([
        api.unreadInputCount(),
        api.unreadTaskCount(),
      ]);
      setUnreadInbox(inboxRes.count);
      setUnreadTasks(tasksRes.count);
      if (!snapshotsTaken.current) {
        setSeenInboxAt(inboxRes.last_seen_at);
        setSeenTasksAt(tasksRes.last_seen_at);
        snapshotsTaken.current = true;
      }
    } catch {
      setUnreadInbox(0);
      setUnreadTasks(0);
    }
  }, []);

  const markInboxViewed = useCallback(async () => {
    setUnreadInbox(0);
    try {
      const res = await api.markInputsSeen();
      setUnreadInbox(res.count);
      setSeenInboxAt(res.last_seen_at);
    } catch {
      loadUnread();
    }
  }, [loadUnread]);

  const markTasksViewed = useCallback(async () => {
    setUnreadTasks(0);
    try {
      const res = await api.markTasksSeen();
      setUnreadTasks(res.count);
      setSeenTasksAt(res.last_seen_at);
    } catch {
      loadUnread();
    }
  }, [loadUnread]);

  useEffect(() => {
    loadUnread();
  }, [loadUnread]);

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
      return;
    }
    setSelectedRunId(link.id);
    setSelectedTaskId(null);
    setTab("runs");
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
    loadUnread();
  }, [tasks, inputs, loadUnread]);

  useEffect(() => {
    if (tab === "tasks" && unreadTasks > 0) markTasksViewed();
    if (tab === "inbox" && unreadInbox > 0) markInboxViewed();
  }, [markInboxViewed, markTasksViewed, tab, unreadInbox, unreadTasks]);

  useEffect(() => {
    if (tab !== "tasks" || document.visibilityState !== "visible") return;
    markTasksViewed();
  }, [markTasksViewed, tab, tasks]);

  useEffect(() => {
    if (tab !== "inbox" || document.visibilityState !== "visible") return;
    markInboxViewed();
  }, [markInboxViewed, tab, inputs]);

  // Refresh unread badges when the app comes back to the foreground. The
  // task/input lists themselves are already refreshed by useAppData on
  // visibilitychange / focus; the badges are owned here, so they need
  // their own listener.
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === "visible") loadUnread();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", loadUnread);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", loadUnread);
    };
  }, [loadUnread]);

  const onTabChange = useCallback(
    (value: string) => {
      setTab(value);
    },
    [],
  );

  const openTask = useCallback((id: string) => {
    pushDeepLink({ kind: "task", id });
    setSelectedTaskId(id);
    setSelectedRunId(null);
    setTab("tasks");
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
      <Topbar theme={theme} onThemeChange={setTheme} />
      <main className="mx-auto max-w-2xl px-4 py-4">
        <Tabs value={tab} onValueChange={onTabChange}>
          <TabsList className="mb-4 grid w-full grid-cols-3">
            <TabsTrigger value="tasks">
              Tasks
              {tasks.length > 0 && (
                <span className="ml-1.5 text-xs text-muted-foreground">
                  {tasks.length}
                </span>
              )}
              {unreadTasks > 0 && (
                <span className="ml-1.5 rounded-full bg-emerald-500 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-white">
                  {unreadTasks}
                </span>
              )}
            </TabsTrigger>
            <TabsTrigger value="inbox">
              Inbox
              {unreadInbox > 0 && (
                <span className="ml-1.5 rounded-full bg-emerald-500 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-white">
                  {unreadInbox}
                </span>
              )}
            </TabsTrigger>
            <TabsTrigger value="runs">
              Runs
              {runsActionable > 0 && (
                <span className="ml-1.5 rounded-full bg-emerald-500 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-white">
                  {runsActionable}
                </span>
              )}
            </TabsTrigger>
          </TabsList>
          <TabsContent value="tasks">
            <TasksPanel
              tasks={tasks}
              onChanged={refresh}
              seenAfter={seenTasksAt}
              selectedTaskId={selectedTaskId}
              onTaskOpen={openTask}
              onSelectedTaskClose={closeSelectedModal}
            />
          </TabsContent>
          <TabsContent value="inbox">
            <InboxPanel
              inputs={inputs}
              onChanged={refresh}
              onLoadMore={loadMoreInputs}
              hasMore={hasMoreInputs}
              seenAfter={seenInboxAt}
            />
          </TabsContent>
          <TabsContent value="runs">
            <RunsPanel
              {...runs}
              selectedRunId={selectedRunId}
              onRunOpen={openRun}
              onSelectedRunClose={closeSelectedModal}
            />
          </TabsContent>
        </Tabs>
      </main>
      <Composer onCreated={refresh} />
      <Toaster />
    </div>
  );
}
