import { useMemo, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface Props {
  // ISO datetime string or null. Emits the same format.
  value: string | null;
  onChange: (next: string | null) => void;
  onSave: () => void;
  // Step is controlled by the parent so it can render a Back arrow into
  // the surrounding Modal's left-action slot when on the time step.
  step: "date" | "time";
  onStepChange: (step: "date" | "time") => void;
}

const WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const DEFAULT_HOUR = 9;
const DEFAULT_MINUTE = 0;
const EOD_HOUR = 23;
const EOD_MINUTE = 45;

// Shared container height so step 1 and step 2 always render at the same
// modal size. Sized to fit the taller step (time display + 224-px clock dial
// + primary action button).
const PICKER_HEIGHT = "h-[340px]";

function pad(n: number) {
  return String(n).padStart(2, "0");
}

function sameDay(a: Date, b: Date) {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function startOfDay(d: Date) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

// 6-row × 7-col grid starting on Monday that contains the given month.
function buildGrid(year: number, month: number): Date[] {
  const first = new Date(year, month, 1);
  const mondayOffset = (first.getDay() + 6) % 7;
  const start = new Date(year, month, 1 - mondayOffset);
  return Array.from(
    { length: 42 },
    (_, i) => new Date(start.getFullYear(), start.getMonth(), start.getDate() + i),
  );
}

function toLocalTime(d: Date | null): string {
  if (!d) return `${pad(DEFAULT_HOUR)}:${pad(DEFAULT_MINUTE)}`;
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function DatePicker({
  value,
  onChange,
  onSave,
  step,
  onStepChange,
}: Props) {
  const parsed = useMemo(() => (value ? new Date(value) : null), [value]);
  const today = useMemo(() => startOfDay(new Date()), []);
  const [viewMonth, setViewMonth] = useState<Date>(() =>
    parsed ? new Date(parsed.getFullYear(), parsed.getMonth(), 1) : today,
  );
  const time = toLocalTime(parsed);

  const emit = (day: Date, hhmm: string) => {
    const [h, m] = hhmm.split(":").map(Number);
    const next = new Date(day.getFullYear(), day.getMonth(), day.getDate(), h, m);
    onChange(next.toISOString());
  };

  const pickDay = (day: Date) => {
    emit(day, time);
    setViewMonth(new Date(day.getFullYear(), day.getMonth(), 1));
  };

  const onTimeChange = (hhmm: string) => {
    if (!parsed) {
      emit(today, hhmm);
    } else {
      emit(parsed, hhmm);
    }
  };

  return (
    <div className={cn("flex flex-col gap-3", PICKER_HEIGHT)}>
      <div className="flex flex-1 items-center justify-center">
        {step === "date" ? (
          <DateContent
            parsed={parsed}
            today={today}
            viewMonth={viewMonth}
            onShiftMonth={(d) =>
              setViewMonth(
                new Date(viewMonth.getFullYear(), viewMonth.getMonth() + d, 1),
              )
            }
            onPickDay={pickDay}
          />
        ) : (
          <TimeContent value={time} onChange={onTimeChange} />
        )}
      </div>

      {step === "date" ? (
        <Button
          type="button"
          onClick={() => onStepChange("time")}
          disabled={parsed === null}
          className="w-full"
        >
          Next
        </Button>
      ) : (
        <Button type="button" onClick={onSave} className="w-full">
          Save
        </Button>
      )}
    </div>
  );
}

function DateContent({
  parsed,
  today,
  viewMonth,
  onShiftMonth,
  onPickDay,
}: {
  parsed: Date | null;
  today: Date;
  viewMonth: Date;
  onShiftMonth: (delta: number) => void;
  onPickDay: (d: Date) => void;
}) {
  const grid = useMemo(
    () => buildGrid(viewMonth.getFullYear(), viewMonth.getMonth()),
    [viewMonth],
  );
  const selectedDay = parsed ? startOfDay(parsed) : null;

  return (
    <div className="w-full rounded-md border bg-background p-2">
      <div className="mb-2 flex items-center justify-between px-1">
        <button
          type="button"
          aria-label="Previous month"
          onClick={() => onShiftMonth(-1)}
          className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <span className="text-sm font-medium">
          {MONTHS[viewMonth.getMonth()]} {viewMonth.getFullYear()}
        </span>
        <button
          type="button"
          aria-label="Next month"
          onClick={() => onShiftMonth(1)}
          className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>

      <div className="mb-1 grid grid-cols-7 gap-0.5 text-center text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {WEEKDAYS.map((w) => (
          <span key={w}>{w}</span>
        ))}
      </div>

      <div className="grid grid-cols-7 gap-0.5">
        {grid.map((d) => {
          const inMonth = d.getMonth() === viewMonth.getMonth();
          const isSelected =
            selectedDay !== null && sameDay(d, selectedDay);
          const isCurrent = sameDay(d, today);
          return (
            <button
              key={d.toISOString()}
              type="button"
              onClick={() => onPickDay(d)}
              className={cn(
                "h-8 rounded-md text-xs transition-colors",
                inMonth ? "text-foreground" : "text-muted-foreground/50",
                !isSelected && "hover:bg-accent",
                isSelected &&
                  "bg-primary text-primary-foreground hover:bg-primary/90",
                !isSelected &&
                  isCurrent &&
                  "ring-1 ring-inset ring-primary",
              )}
            >
              {d.getDate()}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function TimeContent({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const [mode, setMode] = useState<"hour" | "minute">("hour");
  const [rawH, rawM] = value.split(":").map(Number);
  const hour = Number.isFinite(rawH) ? rawH : DEFAULT_HOUR;
  const minute = Number.isFinite(rawM) ? rawM : DEFAULT_MINUTE;

  const setHour = (h: number) => {
    onChange(`${pad(h)}:${pad(minute)}`);
    // Auto-advance to minute mode (Material Design convention).
    setMode("minute");
  };
  const setMinute = (m: number) => {
    onChange(`${pad(hour)}:${pad(m)}`);
  };

  const isEOD = hour === EOD_HOUR && minute === EOD_MINUTE;

  return (
    <div className="flex w-full flex-col items-center gap-3">
      <div className="flex items-center justify-center gap-3">
        <div className="flex items-center gap-1 text-3xl font-medium tabular-nums">
          <button
            type="button"
            onClick={() => setMode("hour")}
            className={cn(
              "rounded-md px-1 transition-colors",
              mode === "hour"
                ? "text-primary"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {pad(hour)}
          </button>
          <span className="text-muted-foreground">:</span>
          <button
            type="button"
            onClick={() => setMode("minute")}
            className={cn(
              "rounded-md px-1 transition-colors",
              mode === "minute"
                ? "text-primary"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {pad(minute)}
          </button>
        </div>

        <button
          type="button"
          onClick={() => onChange(`${pad(EOD_HOUR)}:${pad(EOD_MINUTE)}`)}
          className={cn(
            "rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide transition-colors",
            isEOD
              ? "border-primary bg-primary text-primary-foreground"
              : "border-input text-muted-foreground hover:bg-accent hover:text-foreground",
          )}
        >
          EOD
        </button>
      </div>

      <ClockDial
        mode={mode}
        hour={hour}
        minute={minute}
        onPickHour={setHour}
        onPickMinute={setMinute}
      />
    </div>
  );
}

const DIAL_SIZE = 224;
const DIAL_CENTER = DIAL_SIZE / 2;
const OUTER_R = 90;
const INNER_R = 58;
const CELL = 30;

function angleAt(slot: number) {
  // 12 slots, slot 0 at top (12 o'clock), clockwise.
  return (slot / 12) * 2 * Math.PI - Math.PI / 2;
}

function ringPos(slot: number, radius: number) {
  const a = angleAt(slot);
  return {
    x: DIAL_CENTER + radius * Math.cos(a),
    y: DIAL_CENTER + radius * Math.sin(a),
  };
}

// Returns (slot 1..12, radius) for a given hour value 0..23.
function hourLayout(h: number): { slot: number; radius: number } {
  if (h === 0) return { slot: 12, radius: INNER_R };
  if (h >= 13) return { slot: h - 12, radius: INNER_R };
  return { slot: h, radius: OUTER_R };
}

function minuteLayout(m: number): { slot: number; radius: number } {
  const slot = Math.round(m / 5) % 12;
  return { slot: slot === 0 ? 12 : slot, radius: OUTER_R };
}

interface ClockCell {
  key: string;
  value: number;
  display: string;
  x: number;
  y: number;
}

function ClockDial({
  mode,
  hour,
  minute,
  onPickHour,
  onPickMinute,
}: {
  mode: "hour" | "minute";
  hour: number;
  minute: number;
  onPickHour: (h: number) => void;
  onPickMinute: (m: number) => void;
}) {
  const cells: ClockCell[] = [];
  if (mode === "hour") {
    for (let slot = 1; slot <= 12; slot++) {
      const p = ringPos(slot, OUTER_R);
      cells.push({
        key: `o${slot}`,
        value: slot,
        display: String(slot),
        x: p.x,
        y: p.y,
      });
    }
    for (let slot = 1; slot <= 12; slot++) {
      const p = ringPos(slot, INNER_R);
      const v = slot === 12 ? 0 : slot + 12;
      cells.push({
        key: `i${slot}`,
        value: v,
        display: pad(v),
        x: p.x,
        y: p.y,
      });
    }
  } else {
    for (let slot = 0; slot < 12; slot++) {
      const p = ringPos(slot, OUTER_R);
      const v = slot * 5;
      cells.push({
        key: `m${slot}`,
        value: v,
        display: pad(v),
        x: p.x,
        y: p.y,
      });
    }
  }

  const layout =
    mode === "hour" ? hourLayout(hour) : minuteLayout(minute);
  const hand = ringPos(layout.slot, layout.radius);
  const selectedValue =
    mode === "hour" ? hour : (Math.round(minute / 5) * 5) % 60;

  return (
    <div
      className="relative mx-auto select-none"
      style={{ width: DIAL_SIZE, height: DIAL_SIZE }}
    >
      <div className="absolute inset-0 rounded-full bg-accent/40" />
      <svg
        className="pointer-events-none absolute inset-0"
        width={DIAL_SIZE}
        height={DIAL_SIZE}
      >
        <line
          x1={DIAL_CENTER}
          y1={DIAL_CENTER}
          x2={hand.x}
          y2={hand.y}
          className="stroke-primary"
          strokeWidth={2}
        />
        <circle
          cx={DIAL_CENTER}
          cy={DIAL_CENTER}
          r={3}
          className="fill-primary"
        />
      </svg>
      {cells.map((c) => {
        const isSelected = c.value === selectedValue;
        return (
          <button
            key={c.key}
            type="button"
            onClick={() =>
              mode === "hour" ? onPickHour(c.value) : onPickMinute(c.value)
            }
            style={{
              left: c.x - CELL / 2,
              top: c.y - CELL / 2,
              width: CELL,
              height: CELL,
            }}
            className={cn(
              "absolute inline-flex items-center justify-center rounded-full text-xs tabular-nums transition-colors",
              isSelected
                ? "bg-primary text-primary-foreground"
                : "text-foreground hover:bg-accent",
            )}
          >
            {c.display}
          </button>
        );
      })}
    </div>
  );
}
