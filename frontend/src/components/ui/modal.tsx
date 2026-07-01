import { useEffect, type ReactNode } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  className?: string;
  titleClassName?: string;
  // Optional content for the top-left of the header (e.g. a back arrow).
  // Sized for a 32-px square button to balance the close X on the right.
  leftAction?: ReactNode;
}

export function Modal({
  open,
  onClose,
  title,
  children,
  className,
  titleClassName,
  leftAction,
}: Props) {
  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className={cn(
          "flex w-full max-w-sm flex-col rounded-xl border bg-card p-4 text-card-foreground shadow-lg",
          className,
        )}
      >
        <div className="mb-3 grid shrink-0 grid-cols-[2rem_1fr_2rem] items-center">
          <div className="justify-self-start">{leftAction}</div>
          <div
            className={cn(
              "min-w-0 truncate text-center text-sm font-semibold",
              titleClassName,
            )}
            title={title}
          >
            {title}
          </div>
          <button
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="inline-flex h-7 w-7 shrink-0 items-center justify-center justify-self-end rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
