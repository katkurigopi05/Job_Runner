"use client";

import Link from "next/link";
import { flexRender, type Table } from "@tanstack/react-table";
import type { ApplicationRow } from "@/lib/application-table";
import { StatusPill } from "@/components/status";

export function ApplicationDataTable({
  table,
  label,
  search,
  onSearch,
}: {
  table: Table<ApplicationRow>;
  label: string;
  search: string;
  onSearch: (value: string) => void;
}) {
  return (
    <section aria-label={label} className="space-y-4">
      <label className="grid max-w-md gap-2 text-label text-ink-soft">
        Search {label.toLowerCase()}
        <input
          type="search"
          value={search}
          onChange={(event) => onSearch(event.target.value)}
          placeholder="Company, role or status"
          className="rounded-lg border border-rule bg-paper px-3 py-2 text-ink"
        />
      </label>
      <p role="status" className="text-xs text-ink-faint">
        {table.getRowModel().rows.length} of{" "}
        {table.getCoreRowModel().rows.length} applications
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">
            {label}; select a column heading to sort
          </caption>
          <thead>
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id} className="border-b border-rule">
                {group.headers.map((header) => (
                  <th
                    key={header.id}
                    scope="col"
                    className="px-3 py-3 text-label text-ink-soft"
                    aria-sort={
                      header.column.getIsSorted() === "asc"
                        ? "ascending"
                        : header.column.getIsSorted() === "desc"
                          ? "descending"
                          : "none"
                    }
                  >
                    <button
                      type="button"
                      onClick={header.column.getToggleSortingHandler()}
                      className="rounded px-1 py-1 hover:bg-paper-high focus-visible:outline-2"
                    >
                      {flexRender(
                        header.column.columnDef.header,
                        header.getContext(),
                      )}
                      <span aria-hidden="true">
                        {header.column.getIsSorted() === "asc"
                          ? " ↑"
                          : header.column.getIsSorted() === "desc"
                            ? " ↓"
                            : " ↕"}
                      </span>
                    </button>
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr key={row.id} className="border-b border-rule">
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-3 py-4 align-top">
                    {cell.column.id === "company" ? (
                      <>
                        <Link
                          href={`/applications/${row.original.id}`}
                          className="underline-offset-4 hover:underline"
                        >
                          {row.original.role}
                        </Link>
                        <p className="mt-1 text-xs text-ink-faint">
                          {row.original.company} ·{" "}
                          {row.original.application.ats ?? "unknown ats"}
                        </p>
                      </>
                    ) : cell.column.id === "status" ? (
                      <>
                        <StatusPill
                          status={row.original.application.status}
                          reason={row.original.application.failure_reason}
                        />
                        {row.original.status !==
                        row.original.application.status ? (
                          <p className="mt-1 text-xs text-ink-soft">
                            {row.original.status.replaceAll("_", " ")}
                          </p>
                        ) : null}
                      </>
                    ) : cell.column.id === "score" ? (
                      row.original.score === undefined ? (
                        "Not available"
                      ) : (
                        row.original.score.toFixed(3)
                      )
                    ) : (
                      <time
                        dateTime={new Date(
                          cell.getValue<number>(),
                        ).toISOString()}
                      >
                        {new Date(cell.getValue<number>()).toLocaleDateString(
                          "en-US",
                          { timeZone: "UTC" },
                        )}
                      </time>
                    )}
                  </td>
                ))}
              </tr>
            ))}
            {table.getRowModel().rows.length === 0 ? (
              <tr>
                <td
                  colSpan={table.getAllColumns().length}
                  className="py-12 text-center text-ink-faint"
                >
                  No matching applications.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </section>
  );
}
