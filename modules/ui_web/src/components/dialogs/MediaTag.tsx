import type { MediaItem } from "@/types/api";

function humanSize(bytes: number | null): string {
  if (!bytes) return "";
  const kb = bytes / 1024;
  if (kb < 1024) return `${Math.round(kb)} КБ`;
  return `${(kb / 1024).toFixed(1)} МБ`;
}

function humanDuration(sec: number | null): string {
  if (!sec) return "";
  return `${sec} сек`;
}

export function MediaTag({ m }: { m: MediaItem }) {
  const parts: string[] = [m.type];
  if (m.type === "photo" || m.type === "video" || m.type === "video_note") {
    if (m.width && m.height) parts.push(`${m.width}×${m.height}`);
  }
  if (m.type === "document" && m.file_name) parts.push(m.file_name);
  if (m.duration) parts.push(humanDuration(m.duration));
  const size = humanSize(m.size_bytes);
  if (size) parts.push(size);

  return <div className="mono text-[11px] text-muted-foreground">{parts.join(" · ")}</div>;
}
