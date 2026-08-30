"use client";

import { useEffect, useState } from "react";
import styles from "./nav.module.css";

type Theme = "dark" | "light";

export default function ThemeToggle() {
  // Dark is the SSR'd default (see layout.tsx's inline script); read the
  // real attribute after mount so the toggle reflects a stored preference.
  const [theme, setTheme] = useState<Theme>("dark");

  useEffect(() => {
    const current = document.documentElement.getAttribute("data-theme");
    setTheme(current === "light" ? "light" : "dark");
  }, []);

  function toggle() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("theme", next);
    } catch {
      // Ignore (e.g. privacy mode blocking storage) - the toggle still works
      // for this session, it just won't persist across reloads.
    }
  }

  return (
    <button
      type="button"
      className={styles.themeToggle}
      onClick={toggle}
      aria-label="Toggle dark/light mode"
      title="Toggle dark/light mode"
    >
      {theme === "dark" ? "☀️ Light" : "🌙 Dark"}
    </button>
  );
}
