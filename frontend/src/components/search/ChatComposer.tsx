import { Search } from "lucide-react";
import { Input } from "@/components/ui/input";

export function ChatComposer({
  value,
  onChange,
  onSend,
  streaming,
  onClose,
  chatEnabled,
}: {
  value: string;
  onChange: (value: string) => void;
  onSend: (text: string) => void;
  streaming: boolean;
  onClose: () => void;
  chatEnabled: boolean;
}) {
  const submit = () => {
    const text = value.trim();
    if (!chatEnabled || !text || streaming) return;
    onSend(text);
    onChange("");
  };

  return (
    <div className="relative min-w-0 flex-1">
      <div className="flex h-12 items-center gap-3 rounded-full bg-secondary px-4 shadow-sm">
        <Search className="h-5 w-5 shrink-0 text-muted-foreground" aria-hidden="true" />
        <Input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="Search..."
          enterKeyHint={chatEnabled ? "send" : "search"}
          autoCapitalize="sentences"
          autoCorrect="off"
          autoComplete="off"
          autoFocus
          aria-label="Search"
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              if (chatEnabled) submit();
              else event.currentTarget.blur();
            } else if (event.key === "Escape") {
              onClose();
            }
          }}
          className="h-10 min-w-0 flex-1 border-0 bg-transparent px-0 text-sm shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
        />
      </div>
    </div>
  );
}
