import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium transition-colors",
  {
    variants: {
      variant: {
        default: "border-transparent bg-secondary text-secondary-foreground",
        open:
          "border-transparent bg-emerald-100 text-emerald-800 dark:bg-emerald-500/20 dark:text-emerald-200",
        duplicate:
          "border-transparent bg-amber-100 text-amber-800 dark:bg-amber-500/20 dark:text-amber-200",
        not_task:
          "border-transparent bg-rose-100 text-rose-800 dark:bg-rose-500/20 dark:text-rose-200",
        closed:
          "border-transparent bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200",
        no_change:
          "border-transparent bg-indigo-100 text-indigo-800 dark:bg-indigo-500/20 dark:text-indigo-200",
        overdue:
          "border-transparent bg-red-500 text-white dark:bg-red-500/25 dark:text-red-100",
        urgent:
          "border-transparent bg-orange-500 text-white dark:bg-orange-500/25 dark:text-orange-100",
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
