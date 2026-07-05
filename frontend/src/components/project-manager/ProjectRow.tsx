import { Loader2, Trash2, Eye } from "lucide-react";
import { Button } from "@/components/ui";
import { UploadSplitButton } from "./UploadSplitButton";
import {
  formatBytes,
  formatScheduledAt,
  getLibraryTypeLabel,
  statusCircleClasses,
} from "./utils";
import { isAccountCompatibleWithProjectRow } from "@/utils/libraryTypes";
import type { ProjectManagerRow, Account } from "@/types";

interface ProjectRowProps {
  row: ProjectManagerRow;
  accounts: Account[];
  selectedAccount: Account | null;
  uploadState?: {
    active: boolean;
    label: string | null;
  };
  activeDeleteId: string | null;
  onUpload: (row: ProjectManagerRow) => void;
  onUploadSchedule: (row: ProjectManagerRow) => void;
  onUploadUrgent: (row: ProjectManagerRow) => void;
  onDelete: (row: ProjectManagerRow) => void;
  onPreview: (target: { driveVideoId: string | null; projectId: string; localVideoAvailable: boolean }) => void;
  multiDeleteMode: boolean;
  isSelected: boolean;
  onToggleSelect: (id: string) => void;
}

export function ProjectRow({
  row,
  accounts,
  selectedAccount,
  uploadState,
  activeDeleteId,
  onUpload,
  onUploadSchedule,
  onUploadUrgent,
  onDelete,
  onPreview,
  multiDeleteMode,
  isSelected,
  onToggleSelect,
}: ProjectRowProps) {
  const libraryTypeLabel = getLibraryTypeLabel(row.library_type);
  const compatibleAccounts = accounts.filter((account) =>
    isAccountCompatibleWithProjectRow(account, row),
  );
  const hasCompatibleAccount = compatibleAccounts.length > 0;
  const requiresCompatibleAccount = accounts.length > 0;
  const readinessReasons = row.can_upload_reasons.join("; ");
  const canUpload = row.can_upload_status === "green"
    && row.uploaded_status === "red"
    && (!requiresCompatibleAccount || hasCompatibleAccount);

  const compatibilityReason = requiresCompatibleAccount && !hasCompatibleAccount
    ? row.language
      ? `No account supports ${libraryTypeLabel} in ${row.language.toUpperCase()}`
      : `No account supports ${libraryTypeLabel}`
    : null;

  const uploadDisabledReason = !canUpload
    ? row.uploaded_status !== "red"
      ? "Already uploaded or scheduled"
      : [compatibilityReason, readinessReasons || null]
          .filter(Boolean)
          .join("; ") || "Not ready for upload"
    : null;

  const account = row.scheduled_account_id
    ? accounts.find((a) => a.id === row.scheduled_account_id)
    : null;

  const uploadButton = (
    <UploadSplitButton
      row={row}
      selectedAccount={selectedAccount ?? null}
      uploadActive={!!uploadState?.active}
      uploadLabel={uploadState?.label ?? null}
      disabled={!canUpload}
      disabledReason={uploadDisabledReason ?? undefined}
      onAuto={() => onUpload(row)}
      onSchedule={() => onUploadSchedule(row)}
      onUrgent={() => onUploadUrgent(row)}
    />
  );

  return (
    <tr
      className={`border-b border-[hsl(var(--border))]/50 transition-colors duration-150 ${
        multiDeleteMode && isSelected
          ? "bg-[hsl(var(--destructive))]/10"
          : "hover:bg-[hsl(var(--muted))]/30"
      }`}
    >
      {/* Status */}
      <td className="py-3 pr-3">
        <span
          className={statusCircleClasses(row.uploaded_status)}
          title={
            row.uploaded
              ? "Uploaded"
              : row.uploaded_status === "orange"
                ? "Scheduled"
                : "Not uploaded"
          }
        />
      </td>

      {/* Title — truncate to prevent overflow */}
      <td className="py-3 pr-3 overflow-hidden">
        <div className="font-medium truncate">{row.anime_title || "Unknown"}</div>
        <div className="font-mono text-[11px] tracking-wide text-[hsl(var(--muted-foreground))] truncate">
          {row.project_id}
        </div>
        {row.drive_folder_url && (
          <a
            href={row.drive_folder_url}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-[hsl(var(--primary))] hover:underline"
            onClick={(e) => e.stopPropagation()}
          >
            Drive folder
          </a>
        )}
      </td>

      {/* Account */}
      <td className="py-3 pr-3 overflow-hidden">
        {account ? (
          <div className="flex items-center gap-1.5 min-w-0">
            <img src={account.avatar_url} alt="" className="w-5 h-5 rounded-full object-cover shrink-0" />
            <span className="text-xs truncate">{account.name}</span>
          </div>
        ) : row.scheduled_account_id ? (
          <span className="text-xs text-[hsl(var(--muted-foreground))] truncate block">{row.scheduled_account_id}</span>
        ) : null}
      </td>

      {/* Lang */}
      <td className="py-3 pr-3">
        <span className="text-xs font-medium uppercase text-[hsl(var(--muted-foreground))]">
          {row.language || ""}
        </span>
      </td>

      {/* Type */}
      <td className="py-3 pr-3">
        <span className="text-xs text-[hsl(var(--muted-foreground))]">
          {libraryTypeLabel}
        </span>
      </td>

      {/* LLM preset */}
      <td className="py-3 pr-3">
        <span
          className={`text-xs ${
            row.llm_preset_is_default
              ? "italic text-[hsl(var(--muted-foreground))]"
              : "text-[hsl(var(--foreground))]"
          }`}
          title={row.llm_preset_is_default ? "Default" : "Project override"}
        >
          {row.llm_preset_resolved}
        </span>
      </td>

      {/* Min playback speed */}
      <td className="py-3 pr-3">
        <span
          className={`text-xs font-mono ${
            row.min_playback_speed_is_default
              ? "italic text-[hsl(var(--muted-foreground))]"
              : "text-[hsl(var(--foreground))]"
          }`}
          title={
            row.min_playback_speed_is_default ? "Default" : "Project override"
          }
        >
          {row.min_playback_speed_resolved.toFixed(2)}
        </span>
      </td>

      {/* Template */}
      <td className="py-3 pr-3">
        <span
          className={`text-xs ${
            row.template_is_default
              ? "italic text-[hsl(var(--muted-foreground))]"
              : "text-[hsl(var(--foreground))]"
          }`}
          title={row.template_is_default ? "Default" : "Project override"}
        >
          {row.template_resolved}
        </span>
      </td>

      {/* Scheduled At */}
      <td className="py-3 pr-3">
        <span className="text-xs text-[hsl(var(--muted-foreground))]">
          {formatScheduledAt(row.scheduled_at) || (row.uploaded ? "Uploaded" : "")}
        </span>
      </td>

      {/* Size */}
      <td className="py-3 pr-3 text-sm">{formatBytes(row.local_size_bytes)}</td>

      {/* Actions */}
      <td className="py-3 pr-3">
        <div className="flex items-center gap-1.5">
          {/* Video preview (always reserve space for alignment) */}
          {(row.drive_video_id || row.local_video_available) ? (
            <Button
              size="icon"
              variant="ghost"
              onClick={() =>
                onPreview({
                  driveVideoId: row.drive_video_id,
                  projectId: row.project_id,
                  localVideoAvailable: !!row.local_video_available,
                })
              }
              className="h-9 w-9 active:scale-95 transition-transform shrink-0"
              title="Preview video"
            >
              <Eye className="h-4 w-4" />
            </Button>
          ) : (
            <div className="h-9 w-9 shrink-0" />
          )}

          {/* Upload */}
          {uploadButton}

          {/* Delete */}
          <Button
            size="icon"
            variant="destructive"
            className="relative overflow-hidden h-9 w-9 active:scale-95 transition-transform shrink-0"
            disabled={activeDeleteId !== null}
            onClick={() => onDelete(row)}
            title="Delete project"
          >
            {activeDeleteId === row.project_id ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Trash2 className="h-4 w-4" />
            )}
          </Button>
        </div>
      </td>

      {/* Checkbox (multi-delete mode) */}
      {multiDeleteMode && (
        <td className="py-3 pr-3">
          <input
            type="checkbox"
            checked={isSelected}
            onChange={() => onToggleSelect(row.project_id)}
            className="h-4 w-4 rounded cursor-pointer accent-[hsl(var(--destructive))]"
          />
        </td>
      )}
    </tr>
  );
}
