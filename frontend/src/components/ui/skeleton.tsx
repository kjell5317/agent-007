import { cn } from "@/lib/utils";

export function SkeletonBlock({ className }: { className?: string }) {
  return (
    <div
      className={cn("animate-pulse rounded-md bg-muted", className)}
      aria-hidden
    />
  );
}

export function ModalSkeleton() {
  return (
    <div className="space-y-4">
      <SkeletonBlock className="h-9 w-2/3" />
      <SkeletonBlock className="h-20 w-full" />
      <div className="grid gap-3 sm:grid-cols-2">
        <SkeletonBlock className="h-10 w-full" />
        <SkeletonBlock className="h-10 w-full" />
        <SkeletonBlock className="h-10 w-full" />
        <SkeletonBlock className="h-10 w-full" />
      </div>
      <SkeletonBlock className="h-32 w-full" />
    </div>
  );
}
