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
import { applicationRows, applicationColumns } from "@/lib/application-table";
import { ApplicationDataTable } from "@/components/application-data-table";

const NO_MATCHES: Match[] = [];

export function ApplicationsTable({
  applications,
  matches = NO_MATCHES,
}: {
  applications: Application[];
  matches?: Match[];
}) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [globalFilter, setGlobalFilter] = useState("");
  const data = useMemo(
    () => applicationRows(applications, matches, false),
    [applications, matches],
  );
  const table = useReactTable({
    data,
    columns: applicationColumns,
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
      label="Applications"
      search={globalFilter}
      onSearch={setGlobalFilter}
    />
  );
}
