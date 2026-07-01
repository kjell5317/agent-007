export type DeepLink =
  | { kind: "task"; id: string }
  | { kind: "run"; id: number };

export function parseDeepLink(location: Location = window.location): DeepLink | null {
  const hash = location.hash.replace(/^#/, "").replace(/^\/+/, "");
  const [kind, rawId] = hash.split("/");
  if (kind === "task" && rawId) return { kind, id: decodeURIComponent(rawId) };
  if (kind === "run" && rawId) {
    const id = Number(rawId);
    return Number.isInteger(id) && id > 0 ? { kind, id } : null;
  }

  const params = new URLSearchParams(location.search);
  const taskId = params.get("task");
  if (taskId) return { kind: "task", id: taskId };
  const runId = params.get("run");
  if (runId) {
    const id = Number(runId);
    return Number.isInteger(id) && id > 0 ? { kind: "run", id } : null;
  }
  return null;
}

export function deepLinkHash(link: DeepLink): string {
  return link.kind === "task"
    ? `#task/${encodeURIComponent(link.id)}`
    : `#run/${link.id}`;
}

export function pushDeepLink(link: DeepLink) {
  window.history.pushState(null, "", deepLinkHash(link));
}

export function clearDeepLink() {
  const params = new URLSearchParams(window.location.search);
  params.delete("task");
  params.delete("run");
  const query = params.toString();
  window.history.pushState(null, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
}
