import type { Metadata } from "next";
import localFont from "next/font/local";
import { ApiHealth } from "@/components/api-health";
import { CrawlerSpider } from "@/components/crawler-spider";
import { ThemeToggle } from "@/components/theme-toggle";
import { SideNotes } from "@/components/side-notes";
import { ReleaseWindowName } from "@/components/release-window-name";
import { DashboardShell } from "@/components/dashboard-shell";
import { StatusStreamProvider, type StatusCounts } from "@/components/status-stream";
import { STATUS_ORDER } from "@/components/status";
import { api } from "@/lib/api";
import "./globals.css";

// The fonts are files in the repo (Latin subsets, SIL OFL 1.1, licences beside
// them), so `next build` needs no network: `next/font/google` downloads at
// build time, and a scheduled CI run failed there with no code change.
// Instrument Sans and JetBrains Mono are variable in weight; Newsreader is 400.
const interfaceFont = localFont({ src: "./fonts/InstrumentSans-latin.woff2", weight: "400 700", display: "swap", variable: "--font-instrument-sans" });
const titleFont = localFont({ src: "./fonts/Newsreader-latin.woff2", weight: "400", display: "swap", variable: "--font-newsreader", adjustFontFallback: "Times New Roman" });
const valueFont = localFont({ src: "./fonts/JetBrainsMono-latin.woff2", weight: "400 800", display: "swap", variable: "--font-jetbrains-mono" });

export const metadata: Metadata = {
  title: "Jobrunner",
  description: "Local job-application agent. You approve every application before it is sent.",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Keep navigation usable while the page's ErrorPanel explains an unavailable API.
  const applications = await api.applications().catch(() => null);
  const counts = Object.fromEntries(STATUS_ORDER.map((status) => [status, 0])) as StatusCounts;
  for (const application of applications ?? []) {
    counts[application.status] = (counts[application.status] ?? 0) + 1;
  }
  return (
    <html lang="en" suppressHydrationWarning className={`${interfaceFont.variable} ${titleFont.variable} ${valueFont.variable}`}>
      <body className="transition-colors">
        <ReleaseWindowName />
        {/* The Desk keeps its nested provider so a fresh page read remains its
            fallback when the stream is disconnected. The API shares one watcher. */}
        <StatusStreamProvider initial={counts}>
          <DashboardShell initial={counts} countsKnown={applications !== null}
            services={<><CrawlerSpider /><ApiHealth /></>}
            theme={<ThemeToggle />} notes={<SideNotes />}
            footer={<p>You approve every application before it is sent. Work-authorization answers are copied from your profile word for word, never generated.</p>}>
            {children}
          </DashboardShell>
        </StatusStreamProvider>
      </body>
    </html>
  );
}
