export interface DriveUploadProgressLike {
  step?: string;
  status?: string;
  message?: string | null;
  phase?: string;
  file_count?: number | null;
  files_completed?: number | null;
  total_bytes?: number | null;
  uploaded_bytes?: number | null;
  current_file?: string | null;
  clear_item_count?: number | null;
  clear_items_completed?: number | null;
}

export function formatByteCount(bytes?: number | null): string {
  const value = Number(bytes ?? 0);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"] as const;
  let size = value;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const decimals = unitIndex === 0 ? 0 : 1;
  return `${size.toFixed(decimals)} ${units[unitIndex]}`;
}

export function formatDriveUploadMessage(
  event: DriveUploadProgressLike,
): string | null {
  if (event.step !== "gdrive") {
    return event.message ?? null;
  }

  switch (event.phase) {
    case "manifest": {
      if (event.file_count && event.total_bytes) {
        return `Preparing Drive manifest (${event.file_count} files, ${formatByteCount(event.total_bytes)})`;
      }
      return event.message ?? "Preparing Drive upload...";
    }
    case "clear": {
      const total = Number(event.clear_item_count ?? 0);
      const completed = Number(event.clear_items_completed ?? 0);
      if (total > 0) {
        return `Clearing existing Drive folder (${completed}/${total})`;
      }
      return event.message ?? "Drive folder is already empty.";
    }
    case "upload": {
      const filesCompleted = Number(event.files_completed ?? 0);
      const fileCount = Number(event.file_count ?? 0);
      const uploadedBytes = Number(event.uploaded_bytes ?? 0);
      const totalBytes = Number(event.total_bytes ?? 0);
      if (fileCount > 0 && totalBytes > 0) {
        return `Uploading ${filesCompleted}/${fileCount} files (${formatByteCount(uploadedBytes)} / ${formatByteCount(totalBytes)})`;
      }
      if (fileCount > 0) {
        return `Uploading ${filesCompleted}/${fileCount} files`;
      }
      return event.message ?? "Uploading project to Google Drive...";
    }
    case "persist":
      return "Finishing upload metadata";
    case "complete":
      return event.message ?? "Google Drive upload complete.";
    default:
      return event.message ?? null;
  }
}
