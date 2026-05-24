import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium transition-colors",
  {
    variants: {
      variant: {
        default: "border-transparent bg-secondary text-secondary-foreground",
        open: "border-transparent bg-emerald-100 text-emerald-800",
        duplicate: "border-transparent bg-amber-100 text-amber-800",
        not_task: "border-transparent bg-rose-100 text-rose-800",
        closed: "border-transparent bg-slate-200 text-slate-700",
        no_change: "border-transparent bg-indigo-100 text-indigo-800",
        overdue: "border-transparent bg-rose-100 text-rose-800",
        muted: "border-transparent bg-muted text-muted-foreground",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
