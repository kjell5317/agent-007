import { ArrowUp } from "lucide-react";
import { useState } from "react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export function ChatComposer({
  onSend,
  streaming,
  onClose,
}: {
  onSend: (text: string) => void;
  streaming: boolean;
  onClose: () => void;
}) {
  const [value, setValue] = useState("");

  const submit = () => {
    const text = value.trim();
    if (!text || streaming) return;
    onSend(text);
    setValue("");
  };

  return (
    <div className="fixed inset-x-0 bottom-0 z-40 border-t bg-card pb-[env(safe-area-inset-bottom)] shadow-[0_-4px_14px_rgba(15,23,42,0.06)] dark:shadow-[0_-4px_18px_rgba(0,0,0,0.35)]">
      <div className="mx-auto flex max-w-2xl items-center gap-2 px-3 py-2.5">
        <div className="relative flex-1">
          <Input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Ask anything…"
            enterKeyHint="send"
            autoCapitalize="sentences"
            autoCorrect="off"
            autoComplete="off"
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              } else if (e.key === "Escape") {
                onClose();
              }
            }}
            className="h-10 rounded-full bg-secondary pl-4 pr-11 text-[15px]"
          />
          <button
            type="button"
            onClick={submit}
            disabled={!value.trim() || streaming}
            aria-label="Send"
            className={cn(
              "absolute right-1.5 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-full transition-colors",
              value.trim() && !streaming
                ? "bg-primary text-primary-foreground hover:bg-primary/90"
                : "bg-muted text-muted-foreground",
            )}
          >
            <ArrowUp className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
