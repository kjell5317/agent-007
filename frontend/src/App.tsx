import { useCallback, useEffect, useRef, useState } from "react";
import { Composer } from "@/components/Composer";
import { InboxPanel } from "@/components/inbox/InboxPanel";
import { TasksPanel } from "@/components/tasks/TasksPanel";
import { Topbar } from "@/components/Topbar";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Toaster } from "@/components/ui/sonner";
import { useAppData } from "@/hooks/useAppData";
import { api } from "@/lib/api";

export function App() {
  const { tasks, inputs, refresh, loadMoreInputs, hasMoreInputs } = useAppData();
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
        // The user lands on the Tasks tab by default — count that as a
        // visit so any pre-existing tasks badge clears immediately.
        // Without this, the user has to navigate to Tasks a second time
        // to dismiss the count after starting elsewhere.
        if (tasksRes.count > 0) {
          setUnreadTasks(0);
          api.markTasksSeen().catch(() => {});
        }
      }
    } catch {
      setUnreadInbox(0);
      setUnreadTasks(0);
    }
  }, []);

  useEffect(() => {
    loadUnread();
  }, [loadUnread]);

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
      if (value === "inbox" && unreadInbox > 0) {
        setUnreadInbox(0);
        api.markInputsSeen().catch(() => loadUnread());
      }
      if (value === "tasks") {
        if (unreadTasks > 0) {
          setUnreadTasks(0);
          api.markTasksSeen().catch(() => loadUnread());
        }
        // Switching to Tasks implies leaving Inbox (only two tabs). Anything
        // that landed while the user was on Inbox should count as seen on
        // the way out — symmetric to the "Tasks badge clears on first load
        // because the user is already on that tab" rule.
        setUnreadInbox(0);
        api.markInputsSeen().catch(() => {});
      }
    },
    [unreadInbox, unreadTasks, loadUnread],
  );

  return (
    <div className="min-h-dvh pb-[120px]">
      <Topbar />
      <main className="mx-auto max-w-2xl px-4 py-4">
        <Tabs defaultValue="tasks" onValueChange={onTabChange}>
          <TabsList className="mb-4 grid w-full grid-cols-2">
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
          </TabsList>
          <TabsContent value="tasks">
            <TasksPanel
              tasks={tasks}
              onChanged={refresh}
              seenAfter={seenTasksAt}
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
        </Tabs>
      </main>
      <Composer onCreated={refresh} />
      <Toaster />
    </div>
  );
}
