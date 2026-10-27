/**
 * Thin client for the ugreen-drive backend.
 * Every call throws ApiError on failure so callers can branch on status.
 */

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }

  /**
   * NAS said no — surface the permission-denied UI rather than a generic toast.
   * fsops reports every filesystem denial as exactly "Permission denied"; other
   * 403s are Drive policy refusals whose message should be shown as-is.
   */
  get isPermission() {
    return this.status === 403 && this.message === "Permission denied";
  }

  get isAuth() {
    return this.status === 401;
  }
}

/** Active shared folder name (e.g. "Photos"). Sent as X-Drive-Share on every call; empty lets the server pick. */
let activeShare = "";

export function setActiveShare(share) {
  activeShare = share || "";
}

export function getActiveShare() {
  return activeShare;
}

function withShareQuery(url) {
  if (!activeShare) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}share=${encodeURIComponent(activeShare)}`;
}

async function request(url, options = {}) {
  const { skipShareHeader = false, ...fetchOpts } = options;
  let res;
  try {
    const headers = new Headers(fetchOpts.headers || {});
    // /api/me omits the share header so the server session value is not
    // overwritten by a stale client default on every reload.
    if (activeShare && !skipShareHeader && !headers.has("X-Drive-Share")) {
      headers.set("X-Drive-Share", activeShare);
    }
    // Share in the URL too: browsers/CDNs ignore custom headers for GET cache keys.
    const finalUrl = skipShareHeader ? url : withShareQuery(url);
    res = await fetch(finalUrl, {
      credentials: "same-origin",
      cache: "no-store",
      ...fetchOpts,
      headers,
    });
  } catch (cause) {
    throw new ApiError("Can't reach the drive. Check your connection and try again.", 0);
  }

  if (!res.ok) {
    let detail = res.statusText || `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body && body.detail) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch {
      /* non-JSON error body — keep the status text */
    }
    throw new ApiError(detail, res.status);
  }

  if (res.status === 204) return null;
  const type = res.headers.get("content-type") || "";
  return type.includes("application/json") ? res.json() : res;
}

function form(fields) {
  const data = new FormData();
  for (const [key, value] of Object.entries(fields)) data.set(key, value);
  return data;
}

export function getMe() {
  return request("/api/me", { skipShareHeader: true });
}

export function nasLogin(username, password) {
  return request("/auth/nas", {
    method: "POST",
    skipShareHeader: true,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
}

export function nasOtp(code) {
  return request("/auth/nas/otp", {
    method: "POST",
    skipShareHeader: true,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
}

export function getConfig() {
  return request("/api/config", { skipShareHeader: true });
}

/** Mint a short-lived token to open the LAN origin already signed-in. */
export function mintLanHandoff() {
  return request("/api/lan-handoff", {
    method: "POST",
    skipShareHeader: true,
  });
}

/** Admin Settings (NAS administrators only). */
export function getAdminSettings() {
  return request("/api/admin/settings", { skipShareHeader: true });
}

export function saveAdminSettings(body) {
  return request("/api/admin/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    skipShareHeader: true,
  });
}

export function setShare(share) {
  return request("/api/share", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ share }),
  });
}

export function getUsage() {
  return request("/api/usage");
}

export function unlockPersonal(owner, key, keyFile = false) {
  const body = new FormData();
  body.set("owner", owner);
  body.set("key", key);
  body.set("key_file", String(keyFile));
  return request("/api/personal/unlock", { method: "POST", body, skipShareHeader: true });
}

export function authenticatePersonal(owner, password, code = "") {
  const body = new FormData();
  body.set("owner", owner);
  body.set("password", password);
  body.set("code", code);
  return request("/api/personal/auth", { method: "POST", body, skipShareHeader: true });
}

export function listFiles(path) {
  // share= is appended by request() via withShareQuery — required so each
  // share has a distinct cache key (header alone is not enough).
  return request(`/api/files?path=${encodeURIComponent(path || "")}`);
}

/**
 * Recursive folder content sizes (du-like). Pass relative folder paths.
 * Returns `{ sizes: { [path]: { size, incomplete } } }`.
 */
export function folderSizes(paths) {
  const list = Array.isArray(paths) ? paths.filter((p) => p != null) : [];
  if (!list.length) return Promise.resolve({ sizes: {} });
  const params = new URLSearchParams();
  for (const p of list) params.append("path", p || "");
  return request(`/api/folder-sizes?${params.toString()}`);
}

/** Recursive smart search. `path` scopes the walk ("" = whole share). */
export function searchFiles(q, path = "", limit = 40) {
  const params = new URLSearchParams();
  params.set("q", q || "");
  if (path) params.set("path", path);
  if (limit) params.set("limit", String(limit));
  return request(`/api/search?${params.toString()}`);
}

/** Compare every byte without sending file contents or buffering the entire file. */
export async function uploadFileUnchanged(path, file, signal) {
  const result = await request("/api/upload/fingerprint", {
    method: "POST", body: form({ path, size: file.size }), signal,
  });
  if (!result.fingerprint) return false;
  if (!globalThis.crypto?.subtle) {
    throw new Error("Open Drive over HTTPS to compare existing files before merging.");
  }
  const chunkSize = 8 * 1024 * 1024;
  const digests = new Uint8Array(Math.ceil(file.size / chunkSize) * 32);
  for (let offset = 0, index = 0; offset < file.size; offset += chunkSize, index += 32) {
    signal?.throwIfAborted();
    const bytes = await file.slice(offset, offset + chunkSize).arrayBuffer();
    digests.set(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), index);
  }
  signal?.throwIfAborted();
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", digests));
  return Array.from(hash, (byte) => byte.toString(16).padStart(2, "0")).join("") === result.fingerprint;
}

export function makeDir(path, name) {
  return request("/api/mkdir", { method: "POST", body: form({ path: path || "", name }) });
}

export function renameItem(path, newName) {
  return request("/api/rename", { method: "POST", body: form({ path, new_name: newName }) });
}

export function moveItem(path, dest) {
  return request("/api/move", { method: "POST", body: form({ path, dest: dest || "" }) });
}

export function deleteItem(path) {
  return request("/api/delete", { method: "POST", body: form({ path }) });
}

/** Ask the server to pre-generate thumbs for these paths (fire-and-forget). */
export function warmThumbnails(paths, size = 256) {
  const body = new URLSearchParams();
  for (const p of paths) body.append("path", p);
  body.append("size", String(size));
  return request("/api/thumbnail/warm", { method: "POST", body });
}

/** Permanently wipe #recycle on the active share (NAS admins). */
export function emptyRecycle() {
  return request("/api/recycle/empty", { method: "POST" });
}

export function logout() {
  return request("/auth/logout", { method: "POST" });
}

/** Mint a public expiring link for a file or folder. */
export function createFileLink(path, days) {
  return request("/api/file-link", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, days: Number(days) }),
  });
}

export function downloadUrl(path, { inline = false } = {}) {
  let url = `/api/download?path=${encodeURIComponent(path || "")}`;
  if (inline) url += "&inline=1";
  // FileResponse via <a href> cannot send headers — pass share as query for downloads.
  if (activeShare) url += `&share=${encodeURIComponent(activeShare)}`;
  return url;
}

/** Cached server thumbnail. `mtime` busts the browser cache when the file changes. */
export function thumbnailUrl(path, size = 256, mtime = "") {
  let url = `/api/thumbnail?path=${encodeURIComponent(path || "")}&size=${size}`;
  if (mtime) url += `&mt=${encodeURIComponent(mtime)}`;
  // FileResponse via <img src> cannot send headers — pass share as query.
  if (activeShare) url += `&share=${encodeURIComponent(activeShare)}`;
  return url;
}

/** Synthetic download for the in-app speed test (zeros, not a real file). */
export function speedtestDownloadUrl(size) {
  return `/api/speedtest/download?size=${encodeURIComponent(String(size))}`;
}

/** Max bytes per speed-test / chunked-upload piece (must match server). */
export const UPLOAD_CHUNK_SIZE = 32 * 1024 * 1024;

/** Concurrent streams for multi-part speed tests — saturates the tunnel better. */
export const SPEEDTEST_STREAMS = 8;

/** Parallel files when dropping a folder / multi-select. */
export const UPLOAD_FILE_CONCURRENCY = 4;

/** Parallel chunk PUTs for a single large file. */
export const UPLOAD_CHUNK_STREAMS = 8;

/** Files at or above this size use chunked multi-stream upload. */
export const UPLOAD_CHUNK_THRESHOLD = 8 * 1024 * 1024;

/**
 * Chunk size for one file: at least UPLOAD_CHUNK_STREAMS pieces (whole MiB,
 * 1–32 MiB, the server's limits), so a 20 MB file uses every stream instead
 * of going up as one 32 MiB chunk on a single connection.
 */
export function uploadChunkSize(size) {
  const mib = 1024 * 1024;
  const per = Math.ceil(Math.max(1, size) / UPLOAD_CHUNK_STREAMS);
  return Math.min(UPLOAD_CHUNK_SIZE, Math.max(mib, Math.ceil(per / mib) * mib));
}

/** Large file downloads use parallel Range GETs (same idea as upload streams). */
export const DOWNLOAD_RANGE_THRESHOLD = 8 * 1024 * 1024;
export const DOWNLOAD_RANGE_STREAMS = 8;
export const DOWNLOAD_RANGE_MIN_CHUNK = 4 * 1024 * 1024;

/** Split `total` bytes into up to `maxStreams` inclusive [start, end] ranges. */
export function splitByteRanges(total, maxStreams = DOWNLOAD_RANGE_STREAMS, minChunk = DOWNLOAD_RANGE_MIN_CHUNK) {
  const size = Math.max(0, Math.floor(Number(total) || 0));
  if (size <= 0) return [];
  const n = Math.min(Math.max(1, maxStreams), Math.max(1, Math.ceil(size / minChunk)));
  const parts = [];
  let start = 0;
  for (let i = 0; i < n; i += 1) {
    const remaining = n - i;
    const left = size - start;
    const take = i === n - 1 ? left : Math.floor(left / remaining);
    const end = start + take - 1;
    parts.push({ start, end, index: i });
    start = end + 1;
  }
  return parts;
}

const RESUME_PREFIX = "ifd-upload-resume:";

/** Build a zero-filled Blob without one giant TypedArray. */
function zeroBlob(size) {
  const chunkSize = Math.min(1024 * 1024, size);
  const chunk = new Uint8Array(chunkSize);
  const parts = [];
  let left = size;
  while (left > 0) {
    const n = Math.min(chunkSize, left);
    parts.push(n === chunkSize ? chunk : chunk.subarray(0, n));
    left -= n;
  }
  return new Blob(parts, { type: "application/octet-stream" });
}

/**
 * Upload one speed-test piece (≤32MB) with progress.
 * @returns {{ promise: Promise<{ received: number }>, abort: () => void }}
 */
export function speedtestUploadPiece(size, onProgress) {
  const xhr = new XMLHttpRequest();
  const body = zeroBlob(size);
  let settled = false;

  const promise = new Promise((resolve, reject) => {
    xhr.open("POST", "/api/speedtest/upload");
    xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("Cache-Control", "no-store");
    xhr.timeout = 15 * 60 * 1000;

    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded, event.total);
    });

    xhr.addEventListener("load", () => {
      if (settled) return;
      settled = true;
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText || "{}"));
        } catch {
          resolve({ received: size });
        }
        return;
      }
      let detail = xhr.statusText || `Upload piece failed (${xhr.status})`;
      try {
        const bodyJson = JSON.parse(xhr.responseText || "{}");
        if (bodyJson.detail) detail = typeof bodyJson.detail === "string" ? bodyJson.detail : detail;
      } catch {
        /* keep */
      }
      reject(new ApiError(detail, xhr.status));
    });

    xhr.addEventListener("error", () => {
      if (settled) return;
      settled = true;
      reject(
        new ApiError(
          "Upload stream dropped (network/tunnel). Try a smaller size or re-run — large single connections often reset near the end.",
          0,
        ),
      );
    });
    xhr.addEventListener("abort", () => {
      if (settled) return;
      settled = true;
      reject(new ApiError("Speed test cancelled.", 0));
    });
    xhr.addEventListener("timeout", () => {
      if (settled) return;
      settled = true;
      reject(new ApiError("Upload piece timed out.", 0));
    });

    xhr.send(body);
  });

  return {
    promise,
    abort: () => xhr.abort(),
  };
}

/**
 * Multi-stream upload speed test: split into ≤32MB pieces, run STREAMS in parallel.
 * Much more reliable than one giant 512MB XHR through Cloudflare Tunnel.
 *
 * @returns {{ promise: Promise<{ bytes: number, sec: number, mbps: number }>, abort: () => void }}
 */
export function speedtestUploadMulti(totalBytes, onProgress) {
  // Same piece sizes as a real upload, so small tests still use every stream.
  const pieceSize = uploadChunkSize(totalBytes);
  const pieces = [];
  let left = totalBytes;
  while (left > 0) {
    const n = Math.min(pieceSize, left);
    pieces.push(n);
    left -= n;
  }

  const aborts = [];
  let cancelled = false;
  let loadedTotal = 0;
  const pieceLoaded = pieces.map(() => 0);

  const promise = (async () => {
    const t0 = performance.now();
    let next = 0;

    async function worker() {
      while (!cancelled) {
        const i = next;
        next += 1;
        if (i >= pieces.length) return;
        const size = pieces[i];
        let attempt = 0;
        for (;;) {
          attempt += 1;
          try {
            const { promise: p, abort } = speedtestUploadPiece(size, (loaded) => {
              pieceLoaded[i] = loaded;
              loadedTotal = pieceLoaded.reduce((a, b) => a + b, 0);
              onProgress?.(Math.min(1, loadedTotal / totalBytes));
            });
            aborts.push(abort);
            await p;
            pieceLoaded[i] = size;
            loadedTotal = pieceLoaded.reduce((a, b) => a + b, 0);
            onProgress?.(Math.min(1, loadedTotal / totalBytes));
            break;
          } catch (err) {
            if (cancelled) throw err;
            if (attempt >= 2) throw err;
            pieceLoaded[i] = 0;
            // brief pause then retry this piece once
            await new Promise((r) => setTimeout(r, 400));
          }
        }
      }
    }

    const workers = Array.from({ length: Math.min(SPEEDTEST_STREAMS, pieces.length) }, () => worker());
    await Promise.all(workers);
    const sec = (performance.now() - t0) / 1000;
    return { bytes: totalBytes, sec, mbps: (totalBytes * 8) / sec / 1e6 };
  })();

  return {
    promise,
    abort: () => {
      cancelled = true;
      for (const a of aborts) {
        try {
          a();
        } catch {
          /* ignore */
        }
      }
    },
  };
}

/**
 * Multi-stream download speed test: parallel GETs of equal slices.
 *
 * @returns {Promise<{ bytes: number, sec: number, mbps: number }>}
 */
export async function speedtestDownloadMulti(totalBytes, onProgress) {
  const streams = Math.min(SPEEDTEST_STREAMS, Math.max(1, Math.ceil(totalBytes / (16 * 1024 * 1024))));
  const base = Math.floor(totalBytes / streams);
  const sizes = Array.from({ length: streams }, (_, i) => (i === streams - 1 ? totalBytes - base * (streams - 1) : base));
  const received = sizes.map(() => 0);
  const t0 = performance.now();

  await Promise.all(
    sizes.map(async (size, i) => {
      const res = await fetch(speedtestDownloadUrl(size), {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!res.ok) {
        let detail = res.statusText || `Download stream failed (${res.status})`;
        try {
          const body = await res.json();
          if (body?.detail) detail = typeof body.detail === "string" ? body.detail : detail;
        } catch {
          /* keep */
        }
        throw new ApiError(detail, res.status);
      }
      if (!res.body) {
        const buf = await res.arrayBuffer();
        received[i] = buf.byteLength;
        onProgress?.(Math.min(1, received.reduce((a, b) => a + b, 0) / totalBytes));
        return;
      }
      const reader = res.body.getReader();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        received[i] += value.byteLength;
        onProgress?.(Math.min(1, received.reduce((a, b) => a + b, 0) / totalBytes));
      }
    }),
  );

  const bytes = received.reduce((a, b) => a + b, 0);
  const sec = (performance.now() - t0) / 1000;
  return { bytes, sec, mbps: (bytes * 8) / sec / 1e6 };
}

/**
 * Upload one file with progress reporting (single-stream multipart).
 * Uses XHR because fetch has no upload-progress event.
 *
 * @returns {{promise: Promise<object>, abort: () => void}}
 */
export function uploadFileSimple(path, file, onProgress) {
  const xhr = new XMLHttpRequest();

  const promise = new Promise((resolve, reject) => {
    xhr.open("POST", "/api/upload");
    xhr.withCredentials = true;
    xhr.timeout = 60 * 60 * 1000; // large NAS files through tunnel

    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(event.loaded / event.total);
      }
    });

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText || "{}"));
        } catch {
          resolve({});
        }
        return;
      }
      let detail = xhr.statusText || `Upload failed (${xhr.status})`;
      try {
        const body = JSON.parse(xhr.responseText || "{}");
        if (body && body.detail) {
          detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
        }
      } catch {
        /* keep status text */
      }
      reject(new ApiError(detail, xhr.status));
    });

    xhr.addEventListener("error", () => {
      reject(new ApiError("Upload dropped mid-transfer. Check your connection and try again.", 0));
    });
    xhr.addEventListener("abort", () => {
      reject(new ApiError("Upload cancelled.", 0));
    });
    xhr.addEventListener("timeout", () => {
      reject(new ApiError("Upload timed out.", 0));
    });

    if (activeShare) xhr.setRequestHeader("X-Drive-Share", activeShare);

    const data = new FormData();
    data.set("path", path || "");
    data.set("file", file, file.name);
    xhr.send(data);
  });

  return {
    promise,
    abort: () => xhr.abort(),
  };
}

/** @deprecated use uploadFile (smart) or uploadFileSimple */
export const uploadFileLegacy = uploadFileSimple;

function resumeKey(path, file) {
  return `${RESUME_PREFIX}${activeShare}|${path || ""}|${file.name}|${file.size}|${file.lastModified || 0}`;
}

function loadResumeId(path, file) {
  try {
    return localStorage.getItem(resumeKey(path, file)) || null;
  } catch {
    return null;
  }
}

function saveResumeId(path, file, uploadId) {
  try {
    localStorage.setItem(resumeKey(path, file), uploadId);
  } catch {
    /* private mode */
  }
}

function clearResumeId(path, file) {
  try {
    localStorage.removeItem(resumeKey(path, file));
  } catch {
    /* ignore */
  }
}

function parseXhrError(xhr, fallback) {
  let detail = xhr.statusText || fallback;
  try {
    const body = JSON.parse(xhr.responseText || "{}");
    if (body && body.detail) {
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    }
  } catch {
    /* keep */
  }
  return detail;
}

/**
 * PUT one chunk of a multi-stream upload.
 * @returns {{ promise: Promise<object>, abort: () => void }}
 */
function uploadChunkPut(uploadId, index, blob, onProgress) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    let idleTimer;
    let lastLoaded = 0;
    const resetIdleTimer = () => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => {
        // Reject before abort's event so the caller retries instead of treating
        // a silent connection as a user cancellation.
        reject(new ApiError("Upload chunk stalled. Check your connection and try again.", 0));
        xhr.abort();
      }, 60 * 1000);
    };
    xhr.addEventListener("loadend", () => clearTimeout(idleTimer));
    const url = withShareQuery(
      `/api/upload/chunk?upload_id=${encodeURIComponent(uploadId)}&index=${encodeURIComponent(String(index))}`,
    );
    xhr.open("PUT", url);
    xhr.withCredentials = true;
    xhr.timeout = 30 * 60 * 1000;
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("Cache-Control", "no-store");
    if (activeShare) xhr.setRequestHeader("X-Drive-Share", activeShare);

    xhr.upload.addEventListener("progress", (event) => {
      if (event.loaded > lastLoaded) {
        lastLoaded = event.loaded;
        resetIdleTimer();
      }
      if (event.lengthComputable && onProgress) onProgress(event.loaded, event.total);
    });

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText || "{}"));
        } catch {
          resolve({ index, ok: true });
        }
        return;
      }
      reject(new ApiError(parseXhrError(xhr, `Chunk ${index} failed (${xhr.status})`), xhr.status));
    });
    xhr.addEventListener("error", () => {
      reject(new ApiError("Upload chunk dropped mid-transfer. Retrying…", 0));
    });
    xhr.addEventListener("abort", () => {
      reject(new ApiError("Upload cancelled.", 0));
    });
    xhr.addEventListener("timeout", () => {
      reject(new ApiError("Upload chunk timed out.", 0));
    });

    xhr.send(blob);
    resetIdleTimer();
  });
  return { promise, abort: () => xhr.abort() };
}

async function uploadInit(path, file, chunkSize) {
  let last = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const data = form({
        path: path || "",
        name: file.name,
        size: String(file.size),
        chunk_size: String(chunkSize),
      });
      return await request("/api/upload/init", { method: "POST", body: data });
    } catch (error) {
      last = error;
      const status = error && error.status;
      if ((status !== 500 && status !== 503) || attempt === 2) throw error;
      await new Promise((resolve) => setTimeout(resolve, 200 * (attempt + 1)));
    }
  }
  throw last;
}

async function uploadStatus(uploadId) {
  return request(`/api/upload/status?upload_id=${encodeURIComponent(uploadId)}`);
}

async function uploadComplete(uploadId) {
  return request("/api/upload/complete", {
    method: "POST",
    body: form({ upload_id: uploadId }),
  });
}

async function uploadAbort(uploadId) {
  try {
    await request("/api/upload/abort", {
      method: "POST",
      body: form({ upload_id: uploadId }),
    });
  } catch {
    /* best-effort */
  }
}

/**
 * Chunked multi-stream upload with resume (localStorage upload_id + server status).
 * @returns {{ promise: Promise<object>, abort: () => void }}
 */
export function uploadFileChunked(path, file, onProgress) {
  const aborts = new Set();
  let cancelled = false;
  let uploadId = null;

  const promise = (async () => {
    const chunkSize = uploadChunkSize(file.size);
    let session = null;
    const existing = loadResumeId(path, file);

    if (existing) {
      try {
        const st = await uploadStatus(existing);
        if (st && st.size === file.size && st.name === file.name) {
          session = st;
          uploadId = existing;
        } else {
          clearResumeId(path, file);
        }
      } catch {
        clearResumeId(path, file);
      }
    }

    if (!session) {
      session = await uploadInit(path, file, chunkSize);
      uploadId = session.upload_id;
      saveResumeId(path, file, uploadId);
    }

    const totalChunks = session.total_chunks;
    const cs = session.chunk_size || chunkSize;
    const received = new Set(session.received || []);
    const pieceLoaded = Array.from({ length: totalChunks }, (_, i) => (received.has(i) ? cs : 0));
    // Fix last-chunk size for progress on already-received pieces
    if (received.has(totalChunks - 1)) {
      const lastSize = file.size - cs * (totalChunks - 1);
      pieceLoaded[totalChunks - 1] = Math.max(0, lastSize);
    }

    const report = () => {
      const loaded = pieceLoaded.reduce((a, b) => a + b, 0);
      onProgress?.(Math.min(1, loaded / file.size));
    };
    report();

    const pending = [];
    for (let i = 0; i < totalChunks; i++) {
      if (!received.has(i)) pending.push(i);
    }

    let next = 0;
    async function worker() {
      while (!cancelled) {
        const slot = next;
        next += 1;
        if (slot >= pending.length) return;
        const index = pending[slot];
        const start = index * cs;
        const end = Math.min(file.size, start + cs);
        const blob = file.slice(start, end);
        let attempt = 0;
        for (;;) {
          attempt += 1;
          try {
            if (cancelled) throw new ApiError("Upload cancelled.", 0);
            const { promise: p, abort } = uploadChunkPut(uploadId, index, blob, (loaded) => {
              pieceLoaded[index] = loaded;
              report();
            });
            aborts.add(abort);
            try {
              await p;
            } finally {
              aborts.delete(abort);
            }
            pieceLoaded[index] = end - start;
            report();
            break;
          } catch (err) {
            if (cancelled) throw err;
            if (attempt >= 3) throw err;
            pieceLoaded[index] = 0;
            report();
            await new Promise((r) => setTimeout(r, 300 * attempt));
          }
        }
      }
    }

    const streams = Math.min(UPLOAD_CHUNK_STREAMS, Math.max(1, pending.length));
    try {
      await Promise.all(Array.from({ length: streams }, () => worker()));
    } catch (error) {
      cancelled = true;
      for (const abort of aborts) abort();
      throw error;
    }
    if (cancelled) throw new ApiError("Upload cancelled.", 0);

    const result = await uploadComplete(uploadId);
    clearResumeId(path, file);
    onProgress?.(1);
    return result;
  })();

  return {
    promise,
    abort: () => {
      cancelled = true;
      for (const a of aborts) {
        try {
          a();
        } catch {
          /* ignore */
        }
      }
      if (uploadId) {
        // Keep session for resume — only clear XHRs. Caller may abort hard later.
      }
    },
  };
}

/**
 * Smart upload: small files single-stream; large files chunked + parallel + resume.
 * @returns {{ promise: Promise<object>, abort: () => void }}
 */
export function uploadFile(path, file, onProgress) {
  if (!file || file.size <= 0) {
    return uploadFileSimple(path, file, onProgress);
  }
  if (file.size < UPLOAD_CHUNK_THRESHOLD) {
    return uploadFileSimple(path, file, onProgress);
  }
  return uploadFileChunked(path, file, onProgress);
}

/** Hard-abort a chunked session (discard resume). */
export function discardUploadResume(path, file) {
  const id = loadResumeId(path, file);
  clearResumeId(path, file);
  if (id) return uploadAbort(id);
  return Promise.resolve();
}

/**
 * ZIP download URL for file and/or folder paths (GET ?path=&path=).
 * Folders are expanded server-side into a streamed STORE archive.
 */
export function downloadZipUrl(paths) {
  const q = new URLSearchParams();
  for (const p of paths) q.append("path", p || "");
  if (activeShare) q.set("share", activeShare);
  return `/api/download/zip?${q.toString()}`;
}

function parseContentDisposition(header) {
  if (!header) return null;
  const star = /filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;]+)/i.exec(header);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^["']|["']$/g, ""));
    } catch {
      /* fall through */
    }
  }
  const plain = /filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\s]+)/i.exec(header);
  if (plain) return (plain[1] || plain[2] || "").trim();
  return null;
}

/** Open the Chromium save-file picker during a user gesture (click). */
export function saveFilePicker(filename) {
  if (typeof window.showSaveFilePicker !== "function") return null;
  const zip = /\.zip$/i.test(filename || "");
  try {
    return window.showSaveFilePicker({
      suggestedName: filename || "download",
      ...(zip
        ? {
            types: [
              {
                description: "ZIP archive",
                accept: { "application/zip": [".zip"] },
              },
            ],
          }
        : {}),
    });
  } catch {
    return null;
  }
}

function saveBlobAs(blob, filename) {
  const objectUrl = URL.createObjectURL(blob);
  startBrowserDownload(objectUrl, filename);
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
}

function parseContentRangeTotal(header) {
  if (!header) return 0;
  const m = /\/(\d+)\s*$/.exec(header);
  return m ? Number(m[1]) || 0 : 0;
}

/**
 * Parallel Range GETs into a File System Access writer (seek + write).
 * Returns null if the origin does not honor Range (caller falls back).
 */
async function downloadViaRanges(url, { writer, filename, size, onProgress, signal, share }) {
  const headers = new Headers();
  if (share) headers.set("X-Drive-Share", share);
  headers.set("Range", "bytes=0-0");

  const probe = await fetch(withShareQuery(url), {
    credentials: "same-origin",
    cache: "no-store",
    signal,
    headers,
  });
  if (probe.status !== 206) {
    try {
      await probe.body?.cancel();
    } catch {
      /* ignore */
    }
    return null;
  }
  const total = parseContentRangeTotal(probe.headers.get("Content-Range")) || size;
  try {
    await probe.body?.cancel();
  } catch {
    /* ignore */
  }
  if (total <= 0) return null;

  const cdName = parseContentDisposition(probe.headers.get("Content-Disposition"));
  const finalName = cdName || filename || "download";
  const parts = splitByteRanges(total);
  const loaded = parts.map(() => 0);
  let writeChain = Promise.resolve();

  const writeAt = (position, data) => {
    writeChain = writeChain.then(async () => {
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      await writer.seek(position);
      await writer.write(data);
    });
    return writeChain;
  };

  await Promise.all(
    parts.map(async (part, i) => {
      const rangeHeaders = new Headers();
      if (share) rangeHeaders.set("X-Drive-Share", share);
      rangeHeaders.set("Range", `bytes=${part.start}-${part.end}`);
      const res = await fetch(withShareQuery(url), {
        credentials: "same-origin",
        cache: "no-store",
        signal,
        headers: rangeHeaders,
      });
      if (res.status !== 206 || !res.body) {
        throw new ApiError(res.statusText || `Range download failed (${res.status})`, res.status);
      }
      const reader = res.body.getReader();
      let offset = part.start;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        const pos = offset;
        offset += value.byteLength;
        loaded[i] += value.byteLength;
        await writeAt(pos, value);
        const sum = loaded.reduce((a, b) => a + b, 0);
        onProgress?.({ loaded: sum, total, lengthComputable: true });
      }
    }),
  );

  await writeChain;
  await writer.truncate(total);
  await writer.close();
  onProgress?.({ loaded: total, total, lengthComputable: true });
  return { filename: finalName, size: total };
}

/** Same-origin `<a download>` — browser download manager, not File System Access. */
export function startBrowserDownload(url, filename) {
  const link = document.createElement("a");
  link.href = url;
  link.download = filename || "download";
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
}

/**
 * Download a URL with byte-level progress (and cancel).
 *
 * Prefers the File System Access API so large zips stream to disk instead of
 * buffering in RAM. Falls back to an in-memory blob + save link.
 *
 * onProgress({ loaded, total, lengthComputable })
 * @returns {{ promise: Promise<{filename: string, size: number}>, abort: () => void }}
 */
export function downloadTransfer(url, { filename = "download", onProgress, saveHandlePromise, parallel = false, size = 0 } = {}) {
  const controller = new AbortController();
  let writer = null;

  const abort = () => {
    controller.abort();
    if (writer) {
      try {
        writer.abort();
      } catch {
        /* already closed */
      }
      writer = null;
    }
  };

  const promise = (async () => {
    // Picker must be opened during the click (see saveFilePicker). Awaiting
    // a promise that was started in that gesture is still valid.
    const picker =
      saveHandlePromise ||
      (typeof window.showSaveFilePicker === "function"
        ? window.showSaveFilePicker({
            suggestedName: filename || "download",
          })
        : null);
    if (picker) {
      try {
        const handle = await picker;
        writer = await handle.createWritable();
      } catch (err) {
        if (err && (err.name === "AbortError" || err.name === "NotAllowedError")) {
          throw new ApiError("Download cancelled.", 0);
        }
        writer = null;
      }
    }

    if (!writer) {
      // No disk writer (Safari / picker unavailable). Don't buffer multi-GB
      // zips in RAM — the browser download manager streams to Downloads.
      startBrowserDownload(withShareQuery(url), filename);
      onProgress?.({ loaded: 0, total: 0, lengthComputable: false });
      return { filename, size: 0, native: true };
    }

    if (parallel && size >= DOWNLOAD_RANGE_THRESHOLD) {
      try {
        const ranged = await downloadViaRanges(url, {
          writer,
          filename,
          size,
          onProgress,
          signal: controller.signal,
          share: activeShare,
        });
        if (ranged) {
          writer = null;
          return ranged;
        }
      } catch (err) {
        try {
          await writer.abort();
        } catch {
          /* ignore */
        }
        writer = null;
        if (err?.name === "AbortError" || controller.signal.aborted) {
          throw new ApiError("Download cancelled.", 0);
        }
        if (err instanceof ApiError) throw err;
        throw new ApiError(err?.message || "Download failed", 0);
      }
    }

    const headers = new Headers();
    if (activeShare) headers.set("X-Drive-Share", activeShare);

    let res;
    try {
      res = await fetch(withShareQuery(url), {
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
        headers,
      });
    } catch (err) {
      if (writer) {
        try {
          await writer.abort();
        } catch {
          /* ignore */
        }
        writer = null;
      }
      if (err?.name === "AbortError" || controller.signal.aborted) {
        throw new ApiError("Download cancelled.", 0);
      }
      throw new ApiError("Can't reach the drive. Check your connection and try again.", 0);
    }

    if (!res.ok) {
      let detail = res.statusText || `Download failed (${res.status})`;
      try {
        const body = await res.json();
        if (body && body.detail) {
          detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
        }
      } catch {
        /* non-JSON */
      }
      if (writer) {
        try {
          await writer.abort();
        } catch {
          /* ignore */
        }
        writer = null;
      }
      throw new ApiError(detail, res.status);
    }

    const cdName = parseContentDisposition(res.headers.get("Content-Disposition"));
    const finalName = cdName || filename || "download";
    const totalHeader = Number(res.headers.get("Content-Length")) || 0;
    const body = res.body;

    if (!body) {
      const blob = await res.blob();
      if (writer) {
        await writer.write(blob);
        await writer.close();
        writer = null;
      } else {
        saveBlobAs(blob, finalName);
      }
      onProgress?.({ loaded: blob.size, total: blob.size, lengthComputable: true });
      return { filename: finalName, size: blob.size };
    }

    const reader = body.getReader();
    let loaded = 0;
    const chunks = writer ? null : [];

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        loaded += value.byteLength;
        if (writer) {
          await writer.write(value);
        } else {
          chunks.push(value);
        }
        onProgress?.({
          loaded,
          total: totalHeader,
          lengthComputable: totalHeader > 0,
        });
      }
      if (writer) {
        await writer.close();
        writer = null;
      } else {
        saveBlobAs(new Blob(chunks), finalName);
      }
      onProgress?.({
        loaded,
        total: totalHeader || loaded,
        lengthComputable: true,
      });
      return { filename: finalName, size: loaded };
    } catch (err) {
      if (writer) {
        try {
          await writer.abort();
        } catch {
          /* ignore */
        }
        writer = null;
      }
      if (err?.name === "AbortError" || controller.signal.aborted) {
        throw new ApiError("Download cancelled.", 0);
      }
      if (err instanceof ApiError) throw err;
      throw new ApiError(err?.message || "Download failed", 0);
    }
  })();

  return { promise, abort };
}
