import { Toaster as Sonner } from "sonner";

export function Toaster(props: React.ComponentProps<typeof Sonner>) {
  return (
    <Sonner
      className="toaster group"
      position="bottom-center"
      offset="calc(env(safe-area-inset-bottom, 0px) + 76px)"
      mobileOffset="calc(env(safe-area-inset-bottom, 0px) + 76px)"
      style={{ zIndex: 30 }}
      toastOptions={{
        // Keep the destructive variant tinted for errors; otherwise let
        // Sonner use its default card width / shape / border.
        classNames: {
          toast:
            "group toast group-[.toaster]:border-border group-[.toaster]:bg-card group-[.toaster]:text-card-foreground group-[.toaster]:shadow-lg",
          description: "group-[.toast]:text-muted-foreground",
          error:
            "group-[.toaster]:bg-destructive group-[.toaster]:text-destructive-foreground w-3/4",
        },
      }}
      {...props}
    />
  );
}
