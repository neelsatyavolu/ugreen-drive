/**
 * Per-user, per-share favorites & recents in localStorage.
 * Best-effort storage: private mode / quota errors degrade to empty lists.
 */

const MAX_RECENTS = 15;

let scope = null;

export function setQuickScope(username, share) {
  scope = `${username || "anon"}:${share || "default"}`;
}

function key(kind) {
  return `ifd-quick:${scope}:${kind}`;
}

function readList(kind) {
  if (!scope) return [];
  try {
    const raw = localStorage.getItem(key(kind));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((p) => typeof p === "string") : [];
  } catch {
    return [];
  }
}

function writeList(kind, list) {
  if (!scope) return;
  try {
    localStorage.setItem(key(kind), JSON.stringify(list));
  } catch {
    /* private mode / quota — quick access just won't persist */
  }
}

export function listFavorites() {
  return readList("favorites");
}

export function isFavorite(path) {
  return readList("favorites").includes(path);
}

/** Toggle and return the new favorite state for `path`. */
export function toggleFavorite(path) {
  const current = readList("favorites");
  const next = current.includes(path)
    ? current.filter((p) => p !== path)
    : [...current, path];
  writeList("favorites", next);
  return next.includes(path);
}

export function listRecents() {
  return readList("recents");
}

export function pushRecent(path) {
  if (!path) return;
  const next = [path, ...readList("recents").filter((p) => p !== path)].slice(0, MAX_RECENTS);
  writeList("recents", next);
}

/** Drop a now-stale path from both lists (folder deleted/renamed). */
export function removePath(path) {
  writeList("favorites", readList("favorites").filter((p) => p !== path));
  writeList("recents", readList("recents").filter((p) => p !== path));
}
