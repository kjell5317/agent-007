import { useState, type ComponentPropsWithoutRef, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

export function DisclosureSection({
  title,
  className,
  children,
  ...props
}: {
  title: ReactNode;
  children: ReactNode;
} & Omit<ComponentPropsWithoutRef<"section">, "title" | "children">) {
  const [open, setOpen] = useState(false);

  return (
    <section className={cn("space-y-2 border-t pt-3", className)} {...props}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between text-left text-xs font-medium uppercase text-muted-foreground"
      >
        <span>{title}</span>
        {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
      </button>
      {open && <div className="space-y-2">{children}</div>}
    </section>
  );
}
