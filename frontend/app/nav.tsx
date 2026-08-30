"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import ThemeToggle from "./ThemeToggle";
import styles from "./nav.module.css";

const LINKS = [
  { href: "/trades", label: "Trades" },
  { href: "/calculators", label: "Calculators" },
];

export default function Nav() {
  const pathname = usePathname() ?? "";

  return (
    <nav className={styles.nav}>
      <span className={styles.brand}>Trade Tracker</span>
      {LINKS.map((link) => {
        const isActive = pathname.startsWith(link.href);
        return (
          <Link
            key={link.href}
            href={link.href}
            className={isActive ? `${styles.link} ${styles.linkActive}` : styles.link}
          >
            {link.label}
          </Link>
        );
      })}
      <span className={styles.spacer} />
      <ThemeToggle />
    </nav>
  );
}
