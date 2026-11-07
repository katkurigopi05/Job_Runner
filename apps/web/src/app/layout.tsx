import type { Metadata } from "next";
import { Instrument_Sans, JetBrains_Mono, Newsreader } from "next/font/google";
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

const interfaceFont = Instrument_Sans({ subsets: ["latin"], weight: ["400", "500", "600"], display: "swap", variable: "--font-instrument-sans" });
const titleFont = Newsreader({ subsets: ["latin"], weight: "400", display: "swap", variable: "--font-newsreader" });
const valueFont = JetBrains_Mono({ subsets: ["latin"], weight: ["400", "500"], display: "swap", variable: "--font-jetbrains-mono" });

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
