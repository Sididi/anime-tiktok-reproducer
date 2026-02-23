import { ChevronUp, ChevronDown } from "lucide-react";
import { ProjectRow } from "./ProjectRow";
import type { SortColumn, SortDirection } from "./types";
import type { ProjectManagerRow, Account } from "@/types";

interface ProjectTableProps {
  rows: ProjectManagerRow[];
  accounts: Account[];
  loading: boolean;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
  onToggleSort: (column: SortColumn) => void;
  activeUploadId: string | null;
  activeDeleteId: string | null;
  holdingDeleteId: string | null;
  onUpload: (row: ProjectManagerRow) => void;
  onDeleteHoldStart: (row: ProjectManagerRow) => void;
  onDeleteHoldCancel: () => void;
  onPreview: (driveVideoId: string) => void;
}

const COLUMNS: { key: SortColumn | null; label: string; className?: string }[] = [
  { key: "uploaded", label: "Status", className: "w-16" },
  { key: "anime_title", label: "Anime Title" },
  { key: null, label: "Account", className: "w-32" },
  { key: "language", label: "Lang", className: "w-16" },
  { key: "scheduled_at", label: "Scheduled At", className: "w-36" },
  { key: "local_size_bytes", label: "Size", className: "w-24" },
  { key: null, label: "Actions" },
];

function SortArrow({ column, sortColumn, sortDirection }: { column: SortColumn; sortColumn: SortColumn; sortDirection: SortDirection }) {
  if (column !== sortColumn) return null;
  return sortDirection === "asc" ? (
    <ChevronUp className="h-3.5 w-3.5 inline ml-0.5" />
  ) : (
    <ChevronDown className="h-3.5 w-3.5 inline ml-0.5" />
  );
}

function SkeletonRows() {
  return (
    <>
      {Array.from({ length: 5 }).map((_, i) => (
        <tr key={i} className="border-b border-[hsl(var(--border))]/50">
          <td className="py-3 pr-3"><div className="h-3 w-3 rounded-full bg-[hsl(var(--muted))] animate-pulse" /></td>
          <td className="py-3 pr-3">
            <div className="h-4 w-48 rounded bg-[hsl(var(--muted))] animate-pulse mb-1" />
            <div className="h-3 w-32 rounded bg-[hsl(var(--muted))] animate-pulse" />
          </td>
          <td className="py-3 pr-3"><div className="h-5 w-20 rounded bg-[hsl(var(--muted))] animate-pulse" /></td>
          <td className="py-3 pr-3"><div className="h-3 w-8 rounded bg-[hsl(var(--muted))] animate-pulse" /></td>
          <td className="py-3 pr-3"><div className="h-3 w-24 rounded bg-[hsl(var(--muted))] animate-pulse" /></td>
          <td className="py-3 pr-3"><div className="h-3 w-16 rounded bg-[hsl(var(--muted))] animate-pulse" /></td>
          <td className="py-3 pr-3"><div className="h-8 w-28 rounded bg-[hsl(var(--muted))] animate-pulse" /></td>
        </tr>
      ))}
    </>
  );
}

export function ProjectTable({
  rows,
  accounts,
  loading,
  sortColumn,
  sortDirection,
  onToggleSort,
  activeUploadId,
  activeDeleteId,
  holdingDeleteId,
  onUpload,
  onDeleteHoldStart,
  onDeleteHoldCancel,
  onPreview,
}: ProjectTableProps) {
  const colCount = COLUMNS.length;

  return (
    <table className="w-full text-sm border-collapse">
      <thead>
        <tr className="border-b border-[hsl(var(--border))] text-left">
          {COLUMNS.map((col) => (
            <th key={col.label} className={`py-2 pr-3 ${col.className || ""}`}>
              {col.key ? (
                <button
                  type="button"
                  className="font-medium hover:text-[hsl(var(--primary))] transition-colors"
                  onClick={() => onToggleSort(col.key!)}
                >
                  {col.label}
                  <SortArrow column={col.key} sortColumn={sortColumn} sortDirection={sortDirection} />
                </button>
              ) : (
                <span className="font-medium">{col.label}</span>
              )}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {loading ? (
          <SkeletonRows />
        ) : rows.length === 0 ? (
          <tr>
            <td colSpan={colCount} className="py-8 text-center text-[hsl(var(--muted-foreground))]">
              No local projects found.
            </td>
          </tr>
        ) : (
          rows.map((row) => (
            <ProjectRow
              key={row.project_id}
              row={row}
              accounts={accounts}
              activeUploadId={activeUploadId}
              activeDeleteId={activeDeleteId}
              holdingDeleteId={holdingDeleteId}
              onUpload={onUpload}
              onDeleteHoldStart={onDeleteHoldStart}
              onDeleteHoldCancel={onDeleteHoldCancel}
              onPreview={onPreview}
            />
          ))
        )}
      </tbody>
    </table>
  );
}
