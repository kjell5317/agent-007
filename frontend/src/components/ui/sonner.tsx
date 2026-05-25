import { Toaster as Sonner } from "sonner";

export function Toaster(props: React.ComponentProps<typeof Sonner>) {
  return (
    <Sonner
      position="bottom-center"
      offset="calc(env(safe-area-inset-bottom, 0px) + 80px)"
      toastOptions={{
        classNames: {
          toast:
            "group toast group-[.toaster]:bg-foreground group-[.toaster]:text-background group-[.toaster]:rounded-full group-[.toaster]:border-none",
          error: "group-[.toaster]:bg-destructive group-[.toaster]:text-destructive-foreground",
        },
      }}
      {...props}
    />
  );
}
