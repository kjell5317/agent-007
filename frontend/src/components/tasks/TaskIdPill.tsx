import { toast } from "sonner";

export function TaskIdPill({ id }: { id: string | null }) {
  if (!id) return null;
  return (
    <button
      type="button"
      title={`Copy ${id}`}
      aria-label={`Copy task ID #${id}`}
      onClick={(event) => {
        event.stopPropagation();
        void navigator.clipboard.writeText(id).then(
          () => toast.success(`Copied ${id}`),
          () => toast.error("Could not copy task ID"),
        );
      }}
      className="inline-flex shrink-0 items-center rounded-md bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-foreground"
    >
      #{id}
    </button>
  );
}
