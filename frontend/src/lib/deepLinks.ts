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
  const pathId = location.pathname.match(/^\/([A-Z]{3}\d*)\/?$/)?.[1];
  if (pathId) return { kind: "task", id: pathId };
  return null;
}

export function deepLinkHash(link: DeepLink): string {
  return link.kind === "task"
    ? `#task/${encodeURIComponent(link.id)}`
    : `#run/${link.id}`;
}

// Task ids opened since the modal stack began, oldest first, kept in history
// state so a jump back to one of them rewinds instead of stacking a loop
// (parent → subtask → parent → …).
function taskTrail(): string[] {
  const trail = window.history.state?.taskTrail;
  if (Array.isArray(trail)) return trail;
  const current = parseDeepLink();
  return current?.kind === "task" ? [current.id] : [];
}

export function pushDeepLink(link: DeepLink) {
  const trail = link.kind === "task" ? [...taskTrail(), link.id] : [];
  window.history.pushState({ appDeepLink: true, taskTrail: trail }, "", deepLinkHash(link));
}

export function replaceDeepLink(link: DeepLink) {
  window.history.replaceState({ appDeepLink: true }, "", deepLinkHash(link));
}

// Returns whether `id` was already on the trail and history is rewinding to it.
export function rewindToTask(id: string): boolean {
  const trail = taskTrail();
  const index = trail.lastIndexOf(id);
  // go(0) would reload the page.
  if (index < 0 || index === trail.length - 1) return false;
  window.history.go(index - (trail.length - 1));
  return true;
}

export function backFromDeepLink(): boolean {
  if (window.history.state?.appDeepLink !== true) return false;
  window.history.back();
  return true;
}

export function clearDeepLink() {
  const params = new URLSearchParams(window.location.search);
  params.delete("task");
  params.delete("run");
  const query = params.toString();
  const pathname = /^\/[A-Z]{3}\d*\/?$/.test(window.location.pathname) ? "/" : window.location.pathname;
  window.history.replaceState(null, "", `${pathname}${query ? `?${query}` : ""}`);
}
