import { useCallback, useEffect, useState } from "react";
import { Composer } from "@/components/Composer";
import { InboxPanel } from "@/components/InboxPanel";
import { TasksPanel } from "@/components/TasksPanel";
import { Topbar } from "@/components/Topbar";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Toaster } from "@/components/ui/sonner";
import { useAppData } from "@/hooks/useAppData";
import { api } from "@/lib/api";

export function App() {
  const { tasks, inputs, closedTasks, refresh, loadMoreInputs, hasMoreInputs } = useAppData();
  const [unreadInbox, setUnreadInbox] = useState(0);

  const loadUnread = useCallback(async () => {
    try {
      const r = await api.unreadInputCount();
      setUnreadInbox(r.count);
    } catch {
      setUnreadInbox(0);
    }
  }, []);

  useEffect(() => {
    loadUnread();
  }, [loadUnread]);

  const onTabChange = useCallback(
    (value: string) => {
      if (value === "inbox" && unreadInbox > 0) {
        setUnreadInbox(0);
        api.markInputsSeen().catch(() => loadUnread());
      }
    },
    [unreadInbox, loadUnread],
  );

  const onSyncedAndRefreshUnread = useCallback(async () => {
    await refresh();
    await loadUnread();
  }, [refresh, loadUnread]);

  return (
    <div className="min-h-dvh pb-[120px]">
      <Topbar onSynced={onSyncedAndRefreshUnread} />
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
            <TasksPanel tasks={tasks} onChanged={refresh} />
          </TabsContent>
          <TabsContent value="inbox">
            <InboxPanel
              inputs={inputs}
              closedTasks={closedTasks}
              onChanged={refresh}
              onLoadMore={loadMoreInputs}
              hasMore={hasMoreInputs}
            />
          </TabsContent>
        </Tabs>
      </main>
      <Composer onCreated={refresh} />
      <Toaster />
    </div>
  );
}
