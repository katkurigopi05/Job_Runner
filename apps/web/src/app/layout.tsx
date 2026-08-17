import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Jobrunner",
  description: "Local job-application agent. Nothing submits without your approval.",
};

const NAV = [
  { href: "/", label: "Desk" },
  { href: "/review", label: "Review" },
  { href: "/applications", label: "Pipeline" },
  { href: "/profile", label: "Profile" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-rule">
          <div className="mx-auto flex max-w-5xl flex-wrap items-baseline gap-x-8 gap-y-2 px-6 py-5">
            <Link href="/" className="font-display text-xl tracking-tight">
              jobrunner
            </Link>
            <nav aria-label="Main" className="flex gap-6 font-mono text-xs uppercase tracking-widest">
              {NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="text-ink-soft transition-colors hover:text-ink focus-visible:text-ink focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-attn"
                >
                  {item.label}
                </Link>
              ))}
            </nav>
            <p className="ml-auto font-mono text-xs text-ink-faint">localhost only</p>
          </div>
        </header>
        <main className="mx-auto max-w-5xl px-6 py-10">{children}</main>
        <footer className="mx-auto max-w-5xl px-6 pb-12">
          <p className="border-t border-rule pt-5 font-mono text-xs leading-relaxed text-ink-faint">
            Nothing submits without your approval. Work-authorization answers are copied from your
            profile verbatim, never generated.
          </p>
        </footer>
      </body>
    </html>
  );
}
