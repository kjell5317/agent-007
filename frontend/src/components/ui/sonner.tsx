import { Toaster as Sonner } from "sonner";

export function Toaster(props: React.ComponentProps<typeof Sonner>) {
  return (
    <Sonner
      position="bottom-center"
      // Sit clearly above the Composer (~61px tall above the safe area) so
      // the input field below stays usable while a loading toast is visible.
      offset="calc(env(safe-area-inset-bottom, 0px) + 80px)"
      mobileOffset="calc(env(safe-area-inset-bottom, 0px) + 80px)"
      toastOptions={{
        // Keep the destructive variant tinted for errors; otherwise let
        // Sonner use its default card width / shape / border.
        classNames: {
          error:
            "group-[.toaster]:bg-destructive group-[.toaster]:text-destructive-foreground",
        },
      }}
      {...props}
    />
  );
}
