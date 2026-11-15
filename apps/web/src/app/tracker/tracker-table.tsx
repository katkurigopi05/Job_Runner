"use client";

import { useMemo, useState } from "react";
import {
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import type { Application, Match } from "@/lib/api";
import { applicationRows, trackerColumns } from "@/lib/application-table";
import { ApplicationDataTable } from "@/components/application-data-table";

const NO_MATCHES: Match[] = [];

export function TrackerTable({
  applications,
  matches = NO_MATCHES,
}: {
  applications: Application[];
  matches?: Match[];
}) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [globalFilter, setGlobalFilter] = useState("");
  const data = useMemo(
    () => applicationRows(applications, matches, true),
    [applications, matches],
  );
  const table = useReactTable({
    data,
    columns: trackerColumns,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    getRowId: (row) => row.id,
    globalFilterFn: "includesString",
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });
  return (
    <ApplicationDataTable
      table={table}
      label="Tracker"
      search={globalFilter}
      onSearch={setGlobalFilter}
    />
  );
}
