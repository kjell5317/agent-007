import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { labelChipStyle } from "@/lib/labels";
import type { Label } from "@/lib/types";

interface Props {
  label: Label;
  onClose: () => void;
  onSaved: (label: Label) => void;
}

export function LabelEditModal({ label, onClose, onSaved }: Props) {
  const [description, setDescription] = useState(label.description);
  const [repo, setRepo] = useState(label.github_repo ?? "");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      const updated = await api.updateLabel(label.google_id, {
        description: description.trim(),
        github_repo: repo.trim() || null,
      });
      onSaved(updated);
      onClose();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      titleLabel={`Edit ${label.name}`}
      title={
        <span
          className="inline-flex items-center rounded-full bg-muted px-2.5 py-0.5 text-xs font-medium text-muted-foreground"
          style={labelChipStyle(label.color)}
        >
          {label.name}
        </span>
      }
    >
      <div className="space-y-4">
        <div className="space-y-1.5">
          <label
            htmlFor="label-description"
            className="text-xs font-semibold uppercase tracking-wider text-muted-foreground"
          >
            Description
          </label>
          <Textarea
            id="label-description"
            autoFocus
            rows={4}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What belongs under this label?"
          />
          <p className="text-xs text-muted-foreground">
            This is what the agent matches a new input against. Leave it empty
            to keep the label out of the agent's choices.
          </p>
        </div>

        <div className="space-y-1.5">
          <label
            htmlFor="label-repo"
            className="text-xs font-semibold uppercase tracking-wider text-muted-foreground"
          >
            GitHub repo
          </label>
          <Input
            id="label-repo"
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            placeholder="owner/repo"
            autoCapitalize="none"
            spellCheck={false}
          />
          <p className="text-xs text-muted-foreground">
            Optional. Coding tasks from this repo get this label.
          </p>
        </div>

        <p className="text-xs text-muted-foreground">
          Name and color come from Google Calendar — change them there.
        </p>

        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            className="flex-1"
            onClick={onClose}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button type="button" className="flex-1" onClick={save} disabled={busy}>
            Save
          </Button>
        </div>
      </div>
    </Modal>
  );
}
