import { useState } from "react";
import { Loader2 } from "lucide-react";
import { GroceryComposer } from "@/components/grocery/GroceryComposer";
import { GroceryItemModal } from "@/components/grocery/GroceryItemModal";
import { ShoppingRow, StorageRow } from "@/components/grocery/GroceryRows";
import { RecipesTab } from "@/components/grocery/RecipesTab";
import { StorageItemModal } from "@/components/grocery/StorageItemModal";
import { useGrocery } from "@/hooks/useGrocery";
import { api } from "@/lib/api";
import { zonedDateIso } from "@/lib/dates";
import {
  CATEGORY_LABEL,
  groupByCategory,
  withoutItem,
  withPortionUsed,
  withStaple,
} from "@/lib/grocery";
import { toast } from "@/lib/toast";
import type { GroceryItem, StorageItem } from "@/lib/types";
import { cn } from "@/lib/utils";

export type GroceryTab = "list" | "storage" | "recipes";

export function GroceryPanel({
  tab,
  onTabChange,
  onOpenSettings,
}: {
  tab: GroceryTab;
  onTabChange: (tab: GroceryTab) => void;
  onOpenSettings: () => void;
}) {
  const { state, send } = useGrocery();
  const [editingItem, setEditingItem] = useState<GroceryItem | null>(null);
  const [editingStorage, setEditingStorage] = useState<StorageItem | null>(null);

  if (!state) {
    return (
      <div className="flex justify-center py-12" role="status" aria-label="Loading groceries">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const today = zonedDateIso(new Date());
  const groups = groupByCategory(state.items);

  const crossOff = (item: GroceryItem) => {
    void send(() => api.crossOffGroceryItem(item.id), (s) => withoutItem(s, item.id));
    toast.success(`${item.name} moved to storage`, {
      action: { label: "Undo", onClick: () => void send(() => api.uncrossGroceryItem(item.id)) },
    });
  };

  const toggleStaple = (productKey: string, staple: boolean) =>
    void send(
      () => api.setGroceryStaple(productKey, staple),
      (s) => withStaple(s, productKey, staple),
    );

  const tabs: Array<[GroceryTab, string, number | null]> = [
    ["list", "List", state.items.length],
    ["storage", "Storage", state.storage.length],
    ["recipes", "Recipes", null],
  ];

  return (
    <div className="space-y-3">
      <div role="tablist" className="grid grid-cols-3 gap-1 rounded-full bg-secondary p-1">
        {tabs.map(([id, label, count]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            onClick={() => onTabChange(id)}
            className={cn(
              "rounded-full py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              tab === id ? "bg-background shadow-sm" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {label}
            {count ? <span className="ml-1.5 text-xs tabular-nums text-muted-foreground">{count}</span> : null}
          </button>
        ))}
      </div>

      {tab === "list" ? (
        <>
          <GroceryComposer onAdd={(text) => send(() => api.addGroceryItem(text))} />
          {groups.length === 0 ? (
            <EmptyState>Your shopping list is empty.</EmptyState>
          ) : (
            groups.map((group) => (
              <section key={group.category} aria-label={CATEGORY_LABEL[group.category]}>
                <h2 className="px-1 pb-1.5 pt-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  {CATEGORY_LABEL[group.category]}
                </h2>
                <ul className="divide-y rounded-xl border bg-card">
                  {group.items.map((item) => (
                    <ShoppingRow
                      key={item.id}
                      item={item}
                      onCrossOff={() => crossOff(item)}
                      onEdit={() => setEditingItem(item)}
                      onToggleStaple={() => toggleStaple(item.product_key, !item.staple)}
                    />
                  ))}
                </ul>
              </section>
            ))
          )}
        </>
      ) : tab === "recipes" ? (
        <RecipesTab onOpenSettings={onOpenSettings} />
      ) : state.storage.length === 0 ? (
        <EmptyState>Nothing in storage yet. Items you cross off land here.</EmptyState>
      ) : (
        <ul className="divide-y rounded-xl border bg-card">
          {state.storage.map((item) => (
            <StorageRow
              key={item.id}
              item={item}
              today={today}
              onUse={() => void send(() => api.useStoragePortion(item.id), (s) => withPortionUsed(s, item.id))}
              onEdit={() => setEditingStorage(item)}
              onToggleStaple={() => toggleStaple(item.product_key, !item.staple)}
            />
          ))}
        </ul>
      )}

      {editingItem && (
        <GroceryItemModal
          item={editingItem}
          onClose={() => setEditingItem(null)}
          onSave={(patch) => send(() => api.updateGroceryItem(editingItem.id, patch))}
          onDelete={() => send(() => api.deleteGroceryItem(editingItem.id))}
        />
      )}
      {editingStorage && (
        <StorageItemModal
          item={editingStorage}
          today={today}
          onClose={() => setEditingStorage(null)}
          onSave={(patch) => send(() => api.updateStorageItem(editingStorage.id, patch))}
          onRemove={() => send(() => api.removeStorageItem(editingStorage.id))}
        />
      )}
    </div>
  );
}

function EmptyState({ children }: { children: string }) {
  return (
    <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
      {children}
    </div>
  );
}
