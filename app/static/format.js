/**
 * Presentation helpers: byte sizes, relative dates, and file-kind classification.
 * Pure functions — no DOM, no state.
 */

const UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

/** Human byte size, e.g. 4.71 GB / 212 MB / 38 KB. */
export function formatSize(bytes) {
  if (bytes == null || Number.isNaN(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;

  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const decimals = value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return `${value.toFixed(decimals)} ${UNITS[unit]}`;
}

/**
 * Human transfer rate for UI (bytes/sec → "8.4 MB/s").
 * Slightly coarser decimals than formatSize so the number stays readable/stable.
 */
export function formatSpeed(bytesPerSec) {
  if (bytesPerSec == null || !Number.isFinite(bytesPerSec) || bytesPerSec <= 0) return null;
  if (bytesPerSec < 1024) return `${Math.round(bytesPerSec)} B/s`;
  if (bytesPerSec < 1024 * 1024) {
    const kb = bytesPerSec / 1024;
    return `${kb >= 100 ? Math.round(kb) : kb.toFixed(1)} KB/s`;
  }
  if (bytesPerSec < 1024 * 1024 * 1024) {
    const mb = bytesPerSec / (1024 * 1024);
    return `${mb >= 100 ? Math.round(mb) : mb.toFixed(1)} MB/s`;
  }
  const gb = bytesPerSec / (1024 * 1024 * 1024);
  return `${gb.toFixed(2)} GB/s`;
}

/**
 * Remaining transfer time for UI (seconds → "~12s" / "~3m 20s" / "~1h 05m").
 * Returns null when ETA is unknown or not meaningful yet.
 */
export function formatEta(seconds) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return null;
  // Ignore wild early estimates (stalled / near-zero speed).
  if (seconds > 48 * 3600) return null;
  const s = Math.ceil(seconds);
  if (s < 5) return "a few seconds";
  if (s < 60) return `~${s}s`;
  const m = Math.floor(s / 60);
  const remS = s % 60;
  if (m < 60) {
    return remS === 0 ? `~${m}m` : `~${m}m ${String(remS).padStart(2, "0")}s`;
  }
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return remM === 0 ? `~${h}h` : `~${h}h ${String(remM).padStart(2, "0")}m`;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** Relative for the last week, absolute date beyond that. */
export function formatModified(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";

  const delta = Date.now() - date.getTime();
  if (delta < 0) return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  if (delta < MINUTE) return "Just now";
  if (delta < HOUR) {
    const n = Math.floor(delta / MINUTE);
    return `${n} minute${n === 1 ? "" : "s"} ago`;
  }
  if (delta < DAY) {
    const n = Math.floor(delta / HOUR);
    return `${n} hour${n === 1 ? "" : "s"} ago`;
  }
  if (delta < 2 * DAY) return "Yesterday";
  if (delta < 7 * DAY) return `${Math.floor(delta / DAY)} days ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

/** Full timestamp for tooltips. */
export function formatExact(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString();
}

const EXTENSIONS = {
  video: ["mp4", "mov", "avi", "mkv", "mxf", "m4v", "webm", "mpg", "mpeg", "wmv", "r3d", "braw"],
  image: ["png", "jpg", "jpeg", "gif", "webp", "tif", "tiff", "heic", "svg", "bmp", "dng", "cr2", "arw"],
  audio: ["wav", "mp3", "aac", "aiff", "aif", "m4a", "flac", "ogg"],
  doc: ["pdf", "doc", "docx", "txt", "md", "rtf", "xls", "xlsx", "ppt", "pptx", "csv", "pages", "numbers", "key"],
  zip: ["zip", "rar", "7z", "tar", "gz", "tgz", "dmg", "iso"],
  project: ["aep", "prproj", "drp", "psd", "ai", "indd", "fcpbundle", "veg", "sesx", "blend"],
};

const KIND_BY_EXT = new Map();
for (const [kind, list] of Object.entries(EXTENSIONS)) {
  for (const ext of list) KIND_BY_EXT.set(ext, kind);
}

const KIND_META = {
  folder: { label: "Folder", icon: "#i-folder", color: "var(--brand-green)" },
  recycle: { label: "Recycle", icon: "#i-recycle", color: "hsl(var(--muted-foreground))" },
  video: { label: "Video", icon: "#i-video", color: "var(--brand-amber)" },
  image: { label: "Image", icon: "#i-image", color: "hsl(var(--chart-4))" },
  audio: { label: "Audio", icon: "#i-audio", color: "var(--brand-amber)" },
  doc: { label: "Document", icon: "#i-doc", color: "hsl(var(--muted-foreground))" },
  zip: { label: "Archive", icon: "#i-zip", color: "hsl(var(--muted-foreground))" },
  project: { label: "Project file", icon: "#i-file", color: "hsl(var(--chart-5))" },
  file: { label: "File", icon: "#i-file", color: "hsl(var(--muted-foreground))" },
};

/** Lowercase extension without the leading dot, or "". */
export function fileExt(name) {
  if (!name) return "";
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** True for the share’s Samba/UGOS #recycle folder name. */
export function isRecycleName(name) {
  return Boolean(name) && (name === "#recycle" || name.toLowerCase() === "#recycle");
}

/** Classify an /api/files entry into a kind with label, icon id and accent colour. */
export function describeKind(item) {
  if (item.is_dir) {
    if (isRecycleName(item.name) || item.path === "#recycle") return KIND_META.recycle;
    return KIND_META.folder;
  }
  return KIND_META[KIND_BY_EXT.get(fileExt(item.name)) || "file"];
}

/** Browser-previewable media kinds; null if we should not open a lightbox. */
const PREVIEW_IMAGE = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif"]);
const PREVIEW_VIDEO = new Set(["mp4", "webm", "m4v", "ogg", "ogv", "mov"]);
const PREVIEW_AUDIO = new Set(["mp3", "wav", "ogg", "oga", "m4a", "aac", "flac"]);
const PREVIEW_PDF = new Set(["pdf"]);
const PREVIEW_TEXT = new Set([
  "txt", "md", "markdown", "csv", "tsv", "log", "json", "xml", "yml", "yaml", "rtf", "html", "htm", "css", "js", "ts", "py", "sh",
]);

/**
 * How (if at all) the browser can preview this file in-app.
 * @returns {"image"|"video"|"audio"|"pdf"|"text"|null}
 */
export function previewKind(item) {
  if (!item || item.is_dir) return null;
  const ext = fileExt(item.name);
  if (PREVIEW_IMAGE.has(ext)) return "image";
  if (PREVIEW_VIDEO.has(ext)) return "video";
  if (PREVIEW_AUDIO.has(ext)) return "audio";
  if (PREVIEW_PDF.has(ext)) return "pdf";
  if (PREVIEW_TEXT.has(ext)) return "text";
  return null;
}

/** Split a drive-relative path into segments, dropping empties. */
export function pathParts(path) {
  return (path || "").split("/").filter(Boolean);
}

/**
 * Friendly display name for special NAS folders.
 * Paths / API still use the real on-disk name (e.g. #recycle).
 * Recycle uses a custom SVG icon in the UI — no emoji prefix.
 */
export function displayName(name) {
  if (!name) return name;
  if (isRecycleName(name)) return "Recycle";
  return name;
}

/** True if path is the share’s #recycle folder or anything inside it. */
export function isUnderRecycle(path) {
  const parts = pathParts(path);
  return parts.length > 0 && parts[0] === "#recycle";
}

/** True when browsing the root of #recycle (not a nested trash path). */
export function isRecycleRoot(path) {
  const parts = pathParts(path);
  return parts.length === 1 && parts[0] === "#recycle";
}

/** Label for the current folder, with a generic name at the share root. */
export function folderLabel(path) {
  const parts = pathParts(path);
  return parts.length ? displayName(parts[parts.length - 1]) : "this shared folder";
}
