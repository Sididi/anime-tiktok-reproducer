import { Loader2, Trash2, Eye } from "lucide-react";
import { Button } from "@/components/ui";
import { UploadSplitButton } from "./UploadSplitButton";
import {
  formatBytes,
  formatDateTime,
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
  onPreview: (target: { driveVideoId: string | null; projectId: string }) => void;
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
  const libraryTypeLabel = getLibraryTypeLabel(row.publishing_type ?? row.library_type);
  const compatibleAccounts = accounts.filter((account) =>
    isAccountCompatibleWithProjectRow(account, row),
  );
  const hasCompatibleAccount = compatibleAccounts.length > 0;
  const requiresCompatibleAccount = accounts.length > 0;
  const readinessReasons = row.can_upload_reasons.join("; ");
  const canUpload = !row.archived_at && row.can_upload_status === "green"
    && row.uploaded_status === "red"
    && (!requiresCompatibleAccount || hasCompatibleAccount);

  const compatibilityReason = requiresCompatibleAccount && !hasCompatibleAccount
    ? row.language
      ? `No account supports ${libraryTypeLabel} in ${row.language.toUpperCase()}`
      : `No account supports ${libraryTypeLabel}`
    : null;

  const uploadDisabledReason = !canUpload
    ? row.archived_at
      ? "Archive-only project — disabled"
      : row.uploaded_status !== "red"
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
            row.archived_at
              ? `Status retained at archive time: ${formatDateTime(row.archived_at)}`
              : row.uploaded_status === "publish_error"
              ? `Échec de publication — ${row.publish_error_detail ?? "voir Discord"}`
              : row.uploaded
                ? row.scheduled_at
                  ? `Uploaded ${formatDateTime(row.scheduled_at)}`
                  : "Uploaded"
                : row.uploaded_status === "orange"
                  ? "Scheduled"
                  : "Not uploaded"
          }
        />
      </td>

      {/* Title — truncate to prevent overflow */}
      <td className="py-3 pr-3 overflow-hidden">
        <div className="font-medium truncate">{row.anime_title || "Unknown"}</div>
        {row.archived_at && (
          <div className="text-xs text-[hsl(var(--muted-foreground))]" title={formatDateTime(row.archived_at)}>
            Archive-only · Read-only
          </div>
        )}
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
            {row.archived_at ? "Drive archive" : "Drive folder"}
          </a>
        )}
        {row.archived_at && (
          <details className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">
            <summary className="cursor-pointer">Archive information</summary>
            <div className="space-y-1 mt-1 break-words">
              <div>Archived {formatDateTime(row.archived_at)}</div>
              {row.source_url && <a href={row.source_url} target="_blank" rel="noreferrer" className="block text-[hsl(var(--primary))] hover:underline">Original source</a>}
              {row.mother_project_id && <div>Parent: {row.mother_project_id}</div>}
              {row.source_project_id && <div>Source project: {row.source_project_id}</div>}
              {Object.entries(row.platform_schedules ?? {}).map(([platform, schedule]) => (
                <div key={platform}>
                  {platform}: {formatDateTime(schedule.scheduled_at)}
                  {row.archive_cancellations?.[platform] ? ` · cancellation: ${row.archive_cancellations[platform]}` : ""}
                </div>
              ))}
              {Array.isArray(row.upload_last_result?.platforms) && row.upload_last_result.platforms.map((upload, index) => (
                <div key={`${upload.platform}-${index}`} title={upload.detail}>
                  {upload.platform}: {upload.status ?? "Recorded"}
                  {upload.url && <> · <a href={upload.url} target="_blank" rel="noreferrer" className="text-[hsl(var(--primary))] hover:underline">Post</a></>}
                </div>
              ))}
            </div>
          </details>
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

      {/* Config: LLM • Speed • Template */}
      <td className="py-3 pr-3 overflow-hidden">
        {row.archived_at ? <span className="text-[hsl(var(--muted-foreground))]">—</span> : (
          <div className="text-xs whitespace-nowrap truncate">
            <span
              className={
                row.llm_preset_is_default
                  ? "italic text-[hsl(var(--muted-foreground))]"
                  : "text-[hsl(var(--foreground))]"
              }
              title={`LLM — ${row.llm_preset_is_default ? "Default" : "Project override"}`}
            >
              {row.llm_preset_resolved}
            </span>
            <span className="text-[hsl(var(--muted-foreground))]"> • </span>
            <span
              className={`font-mono ${
                row.min_playback_speed_is_default
                  ? "italic text-[hsl(var(--muted-foreground))]"
                  : "text-[hsl(var(--foreground))]"
              }`}
              title={`Speed — ${row.min_playback_speed_is_default ? "Default" : "Project override"}`}
            >
              {row.min_playback_speed_resolved?.toFixed(2)}
            </span>
            <span className="text-[hsl(var(--muted-foreground))]"> • </span>
            <span
              className={
                row.template_is_default
                  ? "italic text-[hsl(var(--muted-foreground))]"
                  : "text-[hsl(var(--foreground))]"
              }
              title={`Template — ${row.template_is_default ? "Default" : "Project override"}`}
            >
              {row.template_resolved}
            </span>
          </div>
        )}
      </td>

      {/* Scheduled At */}
      <td className="py-3 pr-3">
        <span className="text-xs text-[hsl(var(--muted-foreground))]">
          {formatScheduledAt(row.scheduled_at) || (row.uploaded ? "Uploaded" : "")}
        </span>
        {row.archived_at && Object.keys(row.archive_cancellations ?? {}).length > 0 && (
          <div className="text-xs text-[hsl(var(--muted-foreground))]" title={Object.keys(row.archive_cancellations ?? {}).join(", ")}>
            Pending schedules cancelled
          </div>
        )}
      </td>

      {/* Size */}
      <td className="py-3 pr-3 text-sm">{formatBytes(row.local_size_bytes)}</td>

      {/* Actions */}
      <td className="py-3 pr-3">
        <div className="flex items-center gap-1.5">
          {/* Video preview (always reserve space for alignment) */}
          {!row.archived_at && row.drive_video_id ? (
            <Button
              size="icon"
              variant="ghost"
              onClick={() =>
                onPreview({
                  driveVideoId: row.drive_video_id,
                  projectId: row.project_id,
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
            disabled={!!row.archived_at || !!uploadState?.active || activeDeleteId !== null}
            onClick={() => onDelete(row)}
            title={row.archived_at ? "Archive-only project — already deleted" : "Delete project"}
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
            disabled={!!row.archived_at || activeDeleteId !== null}
            onChange={() => onToggleSelect(row.project_id)}
            className="h-4 w-4 rounded cursor-pointer accent-[hsl(var(--destructive))]"
          />
        </td>
      )}
    </tr>
  );
}
