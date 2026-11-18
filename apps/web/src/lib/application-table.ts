import type { ColumnDef } from "@tanstack/react-table";
import type { Application, Match } from "./api";

export interface ApplicationRow {
  id: string;
  application: Application;
  company: string;
  role: string;
  status: string;
  score: number | undefined;
  created: number;
  updated: number;
}

export function applicationRows(
  applications: Application[],
  matches: Match[] = [],
  outcomes = false,
): ApplicationRow[] {
  // Match URLs alone are not unique across profiles. Never show a different
  // profile's score just because it applied to the same posting.
  const lookup = new Map(
    matches.map((match) => [`${match.profile_id}:${match.url}`, match]),
  );
  return applications.map((application) => {
    const match = lookup.get(`${application.profile_id}:${application.url}`);
    let company = "Unknown company";
    try {
      company = new URL(application.url).hostname;
    } catch {
      /* legacy URL */
    }
    return {
      id: application.id,
      application,
      company,
      role: match?.title ?? application.url,
      status: outcomes
        ? (application.outcome ??
          (application.status === "submitted"
            ? "awaiting"
            : application.status))
        : application.status,
      score: match?.score,
      created: Date.parse(application.created_at),
      updated: Date.parse(application.updated_at),
    };
  });
}

export const applicationColumns: ColumnDef<ApplicationRow>[] = [
  {
    accessorKey: "company",
    header: "Company / Role",
    sortingFn: "alphanumeric",
    accessorFn: (row) => `${row.company} ${row.role}`,
    sortDescFirst: false,
  },
  { accessorKey: "status", header: "Status", sortDescFirst: false },
  { accessorKey: "created", header: "Created", enableGlobalFilter: false },
];
export const trackerColumns: ColumnDef<ApplicationRow>[] = [
  applicationColumns[0],
  applicationColumns[1],
  {
    accessorKey: "score",
    header: "Match Score",
    sortUndefined: "last",
    enableGlobalFilter: false,
  },
  { accessorKey: "updated", header: "Updated", enableGlobalFilter: false },
];
