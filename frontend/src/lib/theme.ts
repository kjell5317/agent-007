import { useCallback, useEffect, useMemo, useState } from "react";

export type ThemePreference = "light" | "dark";

const STORAGE_KEY = "agent-007-theme";

function systemTheme(): ThemePreference {
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function readStoredTheme(): ThemePreference | null {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return stored === "light" || stored === "dark" ? stored : null;
  } catch {
    return null;
  }
}

function writeStoredTheme(theme: ThemePreference) {
  try {
    window.localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Ignore storage failures; the in-memory preference still applies.
  }
}

function applyTheme(theme: ThemePreference) {
  const isDark = theme === "dark";
  document.documentElement.classList.toggle("dark", isDark);
  document.documentElement.style.colorScheme = theme;
}

function getInitialTheme() {
  const stored = readStoredTheme();
  return {
    explicit: stored !== null,
    theme: stored ?? systemTheme(),
  };
}

export function initializeTheme() {
  applyTheme(getInitialTheme().theme);
}

export function useThemePreference() {
  const initial = useMemo(getInitialTheme, []);
  const [theme, setTheme] = useState<ThemePreference>(initial.theme);
  const [explicit, setExplicit] = useState(initial.explicit);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    if (explicit) return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event: MediaQueryListEvent) => {
      setTheme(event.matches ? "dark" : "light");
    };
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [explicit]);

  const setPreference = useCallback((next: ThemePreference) => {
    writeStoredTheme(next);
    setExplicit(true);
    setTheme(next);
  }, []);

  return { theme, setTheme: setPreference };
}
