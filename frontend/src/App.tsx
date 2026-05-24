import { useMemo } from "react";
import { Composer } from "@/components/Composer";
import { InboxPanel } from "@/components/InboxPanel";
import { TasksPanel } from "@/components/TasksPanel";
import { Topbar } from "@/components/Topbar";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Toaster } from "@/components/ui/sonner";
import { useAppData } from "@/hooks/useAppData";

export function App() {
  const { tasks, inputs, closedTasks, refresh } = useAppData();

  const inboxCount = useMemo(
    () =>
      inputs.filter(
        (r) =>
          r.status === "not_task" ||
          r.status === "duplicate" ||
          r.agent_trace?.outcome === "no_change",
      ).length + closedTasks.length,
    [inputs, closedTasks],
  );

  return (
    <div className="min-h-dvh pb-[120px]">
      <Topbar onSynced={refresh} />
      <main className="mx-auto max-w-2xl px-4 py-4">
        <Tabs defaultValue="tasks">
          <TabsList className="mb-4 grid w-full grid-cols-2">
            <TabsTrigger value="tasks">
              Tasks
              {tasks.length > 0 && (
                <span className="ml-1.5 text-xs text-muted-foreground">{tasks.length}</span>
              )}
            </TabsTrigger>
            <TabsTrigger value="inbox">
              Inbox
              {inboxCount > 0 && (
                <span className="ml-1.5 text-xs text-muted-foreground">{inboxCount}</span>
              )}
            </TabsTrigger>
          </TabsList>
          <TabsContent value="tasks">
            <TasksPanel tasks={tasks} onChanged={refresh} />
          </TabsContent>
          <TabsContent value="inbox">
            <InboxPanel inputs={inputs} closedTasks={closedTasks} onChanged={refresh} />
          </TabsContent>
        </Tabs>
      </main>
      <Composer onCreated={refresh} />
      <Toaster />
    </div>
  );
}
