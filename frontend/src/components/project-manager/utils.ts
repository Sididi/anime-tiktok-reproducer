export function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[idx]}`;
}

export function formatScheduledAt(isoString: string | null): string {
  if (!isoString) return "";
  const date = new Date(isoString);
  if (isNaN(date.getTime())) return "";
  const now = new Date();
  if (date <= now) return "Uploaded";
  const day = date.getDate();
  const month = date.toLocaleString("en", { month: "short" });
  const hours = date.getHours().toString().padStart(2, "0");
  const minutes = date.getMinutes().toString().padStart(2, "0");
  return `${day} ${month} ${hours}:${minutes}`;
}

export function statusCircleClasses(color: "green" | "orange" | "red"): string {
  const base = "h-3 w-3 rounded-full inline-block";
  if (color === "green") return `${base} bg-green-500 shadow-[0_0_6px_rgba(34,197,94,0.5)]`;
  if (color === "orange") return `${base} bg-amber-500 shadow-[0_0_6px_rgba(245,158,11,0.5)]`;
  return `${base} bg-red-500 shadow-[0_0_6px_rgba(239,68,68,0.5)]`;
}
