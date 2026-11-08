"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Icon, type IconName } from "@/components/icon";
import { useStatusCounts, type StatusCounts } from "@/components/status-stream";

type Layout = "sidebar" | "top";
const KEY = "jobrunner-layout";
const NAV: { href: string; label: string; icon: IconName; group?: string; separator?: boolean }[] = [
  { href: "/", label: "Desk", icon: "desk" },
  { href: "/review", label: "Review", icon: "review" },
  { href: "/finish", label: "Finish", icon: "finish" },
  { href: "/matches", label: "Matches", icon: "matches", group: "Postings" },
  { href: "/swipe", label: "Rate", icon: "rate" },
  { href: "/label", label: "Grade", icon: "grade" },
  { href: "/ranking", label: "Ranking", icon: "ranking" },
  { href: "/applications", label: "Pipeline", icon: "pipeline", group: "Applications" },
  { href: "/resumes", label: "Résumés", icon: "resumes" },
  { href: "/tracker", label: "Tracker", icon: "tracker" },
  { href: "/chat", label: "Assistant", icon: "assistant", separator: true },
  { href: "/profile", label: "Profile", icon: "profile" },
  { href: "/setup", label: "Setup", icon: "setup" },
];

export function DashboardShell({ children, services, theme, notes, footer, initial, countsKnown }: {
  children: React.ReactNode;
  services: React.ReactNode;
  theme: React.ReactNode;
  notes: React.ReactNode;
  footer: React.ReactNode;
  initial: StatusCounts;
  countsKnown: boolean;
}) {
  const pathname = usePathname();
  const [layout, setLayout] = useState<Layout>("sidebar");
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const { counts, live } = useStatusCounts(initial);
  const waiting = (counts.needs_review ?? 0) + (counts.needs_otp ?? 0);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(KEY);
      if (saved === "sidebar" || saved === "top") setLayout(saved);
    } catch { /* Layout still works when browser storage is unavailable. */ }
  }, []);

  useEffect(() => {
    if (!menuOpen) return;
    function close(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      setMenuOpen(false);
      menuButton.current?.focus();
    }
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [menuOpen]);

  function choose(next: Layout) {
    setLayout(next);
    try { localStorage.setItem(KEY, next); } catch { /* Keep this session's choice. */ }
  }

  return (
    <div className="app-shell" data-layout={layout} data-menu-open={menuOpen}>
      <aside className="app-sidebar">
        <div className="sidebar-brand">
          <Link href="/" onClick={() => setMenuOpen(false)}>jobrunner</Link>
          <button ref={menuButton} type="button" className="mobile-menu-button"
            aria-label={menuOpen ? "Close navigation" : "Open navigation"}
            aria-expanded={menuOpen} aria-controls="dashboard-navigation"
            onClick={() => setMenuOpen((open) => !open)}>
            <Icon name={menuOpen ? "close" : "menu"} />
          </button>
        </div>
        <nav id="dashboard-navigation" className="app-nav" aria-label="Main">
          {NAV.map((item) => (
            <div key={item.href} className="contents">
              {item.group ? <p className="nav-group">{item.group}</p> : null}
              {item.separator ? <div className="nav-separator" role="separator" /> : null}
              <Link href={item.href} className="nav-link" onClick={() => setMenuOpen(false)}
                aria-current={(item.href === "/" ? pathname === "/" : pathname.startsWith(item.href)) ? "page" : undefined}>
                <Icon name={item.icon} />{item.label}
                {item.href === "/review" && (countsKnown || live) && waiting > 0 ? (
                  <span className="ml-auto inline-flex items-center gap-2 font-mono text-xs text-attn-text tabular-nums">
                    <span aria-hidden="true" className="status-dot bg-attn" />{waiting.toLocaleString("en-US")}
                  </span>
                ) : null}
              </Link>
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="sidebar-service space-y-2">{services}</div>
          {theme}
          {notes}
          <div role="group" aria-label="Navigation layout" className="layout-switch">
            <button type="button" className="layout-choice" aria-label="Sidebar layout"
              aria-pressed={layout === "sidebar"} onClick={() => choose("sidebar")}>
              <Icon name="sidebar" />Sidebar
            </button>
            <button type="button" className="layout-choice" aria-label="Top navigation layout"
              aria-pressed={layout === "top"} onClick={() => choose("top")}>
              <Icon name="top" />Top nav
            </button>
          </div>
        </div>
      </aside>
      <div className="content-column">
        <main className="content-width">{children}</main>
        <footer className="content-width app-footer">{footer}</footer>
      </div>
    </div>
  );
}
