import { Loader2, UploadCloud, Trash2, Eye } from "lucide-react";
import { Button } from "@/components/ui";
import { Tooltip } from "./Tooltip";
import { formatBytes, formatScheduledAt, statusCircleClasses } from "./utils";
import type { ProjectManagerRow, Account } from "@/types";

interface ProjectRowProps {
  row: ProjectManagerRow;
  accounts: Account[];
  activeUploadId: string | null;
  activeDeleteId: string | null;
  holdingDeleteId: string | null;
  onUpload: (row: ProjectManagerRow) => void;
  onDeleteHoldStart: (row: ProjectManagerRow) => void;
  onDeleteHoldCancel: () => void;
  onPreview: (driveVideoId: string) => void;
}

export function ProjectRow({
  row,
  accounts,
  activeUploadId,
  activeDeleteId,
  holdingDeleteId,
  onUpload,
  onDeleteHoldStart,
  onDeleteHoldCancel,
  onPreview,
}: ProjectRowProps) {
  const canUpload = row.can_upload_status === "green" && row.uploaded_status === "red";

  const uploadDisabledReason = !canUpload
    ? row.uploaded_status !== "red"
      ? "Already uploaded or scheduled"
      : row.can_upload_reasons.join("; ") || "Not ready for upload"
    : null;

  const account = row.scheduled_account_id
    ? accounts.find((a) => a.id === row.scheduled_account_id)
    : null;

  const uploadButton = (
    <Button
      size="sm"
      onClick={() => onUpload(row)}
      disabled={!canUpload || activeUploadId !== null}
      className="active:scale-95 transition-transform"
    >
      {activeUploadId === row.project_id ? (
        <Loader2 className="h-4 w-4 animate-spin" />
      ) : (
        <>
          <UploadCloud className="h-4 w-4 mr-1.5" />
          Upload
        </>
      )}
    </Button>
  );

  return (
    <tr className="border-b border-[hsl(var(--border))]/50 hover:bg-[hsl(var(--muted))]/30 transition-colors duration-150">
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

      {/* Anime Title */}
      <td className="py-3 pr-3">
        <div className="font-medium">{row.anime_title || "Unknown"}</div>
        <div className="font-mono text-[11px] tracking-wide text-[hsl(var(--muted-foreground))]">
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
      <td className="py-3 pr-3">
        {account ? (
          <div className="flex items-center gap-1.5">
            <img src={account.avatar_url} alt="" className="w-5 h-5 rounded-full object-cover" />
            <span className="text-xs">{account.name}</span>
          </div>
        ) : row.scheduled_account_id ? (
          <span className="text-xs text-[hsl(var(--muted-foreground))]">{row.scheduled_account_id}</span>
        ) : null}
      </td>

      {/* Lang */}
      <td className="py-3 pr-3">
        <span className="text-xs font-medium uppercase text-[hsl(var(--muted-foreground))]">
          {row.language || ""}
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
          {row.drive_video_id ? (
            <Button
              size="icon"
              variant="ghost"
              onClick={() => onPreview(row.drive_video_id!)}
              className="h-9 w-9 active:scale-95 transition-transform"
              title="Preview video"
            >
              <Eye className="h-4 w-4" />
            </Button>
          ) : (
            <div className="h-9 w-9" />
          )}

          {/* Upload */}
          {uploadDisabledReason ? (
            <Tooltip text={uploadDisabledReason}>
              <div>{uploadButton}</div>
            </Tooltip>
          ) : (
            uploadButton
          )}

          {/* Delete (hold 1s) */}
          <Button
            size="icon"
            variant="destructive"
            className="relative overflow-hidden h-9 w-9 active:scale-95 transition-transform"
            disabled={activeDeleteId !== null}
            onMouseDown={() => onDeleteHoldStart(row)}
            onMouseUp={onDeleteHoldCancel}
            onMouseLeave={onDeleteHoldCancel}
            onTouchStart={() => onDeleteHoldStart(row)}
            onTouchEnd={onDeleteHoldCancel}
            onTouchCancel={onDeleteHoldCancel}
            title="Hold to delete"
          >
            <span
              className="absolute inset-0 bg-white/20 origin-left transition-transform"
              style={{
                transform: holdingDeleteId === row.project_id ? "scaleX(1)" : "scaleX(0)",
                transitionDuration: holdingDeleteId === row.project_id ? "1s" : "0s",
                transitionTimingFunction: "linear",
              }}
            />
            {activeDeleteId === row.project_id ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Trash2 className="h-4 w-4" />
            )}
          </Button>
        </div>
      </td>
    </tr>
  );
}
