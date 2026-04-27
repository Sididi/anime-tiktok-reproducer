export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  const decimals = value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return `${value.toFixed(decimals)} ${units[i]}`;
}

export function formatSpeed(mibPerSec: number | null | undefined): string | null {
  if (mibPerSec == null || !Number.isFinite(mibPerSec) || mibPerSec <= 0) {
    return null;
  }
  if (mibPerSec >= 1024) {
    return `${(mibPerSec / 1024).toFixed(2)} GB/s`;
  }
  if (mibPerSec >= 100) return `${mibPerSec.toFixed(0)} MB/s`;
  if (mibPerSec >= 10) return `${mibPerSec.toFixed(1)} MB/s`;
  return `${mibPerSec.toFixed(2)} MB/s`;
}

export function formatEta(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return null;
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const remSec = Math.round(seconds % 60);
  if (minutes < 60) {
    return remSec > 0 ? `${minutes}m ${remSec}s` : `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const remMin = minutes % 60;
  return remMin > 0 ? `${hours}h ${remMin}m` : `${hours}h`;
}

export interface NetworkProgressFields {
  network_bytes_transferred?: number | null;
  network_bytes_total?: number | null;
  network_mib_per_sec?: number | null;
  network_eta_seconds?: number | null;
  network_active_transfers?: number | null;
}

export function formatNetworkProgressLine(
  data: NetworkProgressFields,
): string | null {
  const total = data.network_bytes_total;
  const done = data.network_bytes_transferred;
  if (total == null || total <= 0 || done == null) return null;
  const parts = [`${formatBytes(done)} / ${formatBytes(total)}`];
  const speed = formatSpeed(data.network_mib_per_sec);
  if (speed) parts.push(speed);
  const eta = formatEta(data.network_eta_seconds);
  if (eta) parts.push(`ETA ${eta}`);
  return parts.join(" · ");
}
