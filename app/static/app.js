/**
 * ugreen-drive — file browser front end.
 * Talks to the FastAPI backend in api.js and renders the Machine & Ink UI.
 */
import { bindEmailSignIn } from "./email-sign-in.js?v=20261027-mac";
import * as api from "./api.js?v=20261027-mac";
import { ApiError } from "./api.js?v=20261027-mac";
import {
  describeKind,
  displayName,
  fileExt,
  folderLabel,
  formatEta,
  formatExact,
  formatModified,
  formatSize,
  formatSpeed,
  isRecycleName,
  isRecycleRoot,
  isUnderRecycle,
  pathParts,
  previewKind,
} from "./format.js?v=20261027-mac";
import { $, el, icon, show } from "./dom.js?v=20261027-mac";
import { openSettingsModal } from "./settings.js?v=20261027-mac";
import {
  setQuickScope,
  listFavorites,
  isFavorite,
  toggleFavorite,
  listRecents,
  pushRecent,
  removePath,
} from "./quick.js?v=20261027-mac";

const THEME_KEY = "ifd-theme";
const VIEW_KEY = "ifd-view";
const DENSITY_KEY = "ifd-density";
const HIDE_DOTFILES_KEY = "ifd-hide-dotfiles";
const SORT_KEY = "ifd-sort-key";
const SORT_DIR_KEY = "ifd-sort-dir";
const SHARE_KEY = "ifd-share";
/** When "1", stay on the public tunnel host even if LAN is reachable. */
const FORCE_CLOUD_KEY = "ifd-force-cloud";
const SIDEBAR_W_KEY = "ifd-sidebar-w";
const SIDEBAR_W_DEFAULT = 240;
const SIDEBAR_W_MIN = 180;
const SIDEBAR_W_MAX = 480;

/* ---------------------------------------------------------------------------
   Local storage helpers ($/el/icon/show live in dom.js)
   --------------------------------------------------------------------------- */
function readStored(key, fallback) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode — preference just won't persist */
  }
}

const SORT_KEYS = new Set(["name", "kind", "size", "mtime"]);
const SORT_LABELS = { name: "Name", kind: "Kind", size: "Size", mtime: "Date" };

function storedSortKey() {
  const raw = readStored(SORT_KEY, "name");
  return SORT_KEYS.has(raw) ? raw : "name";
}

function isApplePlatform() {
  return /Mac|iPhone|iPad|iPod/.test(navigator.platform || "")
    || (navigator.userAgentData?.platform === "macOS")
    || /Mac OS X/.test(navigator.userAgent || "");
}

function modGlyph() {
  return isApplePlatform() ? "⌘" : "Ctrl";
}

/* ---------------------------------------------------------------------------
   State
   --------------------------------------------------------------------------- */
const state = {
  me: null,
  path: "",
  items: [],
  loading: false,
  error: null,
  selection: new Set(),
  anchorIndex: null,
  /** Index into visibleItems() for keyboard navigation; null = no cursor. */
  cursorIndex: null,
  query: "",
  view: readStored(VIEW_KEY, "list"),
  density: readStored(DENSITY_KEY, "comfortable"),
  /** Hide names starting with "." (dot files) from the listing. */
  hideDotfiles: readStored(HIDE_DOTFILES_KEY, "0") === "1",
  sortKey: storedSortKey(),
  sortAsc: readStored(SORT_DIR_KEY, "1") !== "0",
  openMenu: null,
  /** Cursor point for context-menu open; null = anchor to ⋯ button. */
  menuAnchor: null,
  shortcuts: [],
  /** Cached folder children for the sidebar tree: path → dir items[] */
  treeChildren: Object.create(null),
  treeExpanded: new Set(),
  treeLoading: new Set(),
  /** Active shared folder (Photos, Documents, …). Empty until /api/me picks one. */
  share: "",
  shares: [],
  isAdmin: false,
  /** Active share has a UGOS/Samba #recycle folder. */
  hasRecycle: false,
  /** Share-switcher menu open (crumb dropdown). */
  shareMenuOpen: false,
  uploads: [],
  uploadRunning: false,
  downloads: [],
  downloadRunning: false,
  /** Paths currently being dragged for in-app moves (null when idle). */
  dndPaths: null,
  suppressClick: false,
  /** True only after loading has been slow enough to justify the skeleton. */
  showSkeleton: false,
};

/** Live recursive search typeahead (panel under the search field). */
const searchUi = {
  open: false,
  loading: false,
  /** "here" = current folder subtree; "all" = whole share. */
  scope: "here",
  results: [],
  active: -1,
  error: null,
  truncated: false,
  scanned: 0,
  requestId: 0,
};

let toastTimer = null;
let dragDepth = 0;
let skeletonTimer = null;
let loadRequestId = 0;
let searchTimer = null;
/** Don't flash the skeleton for sub-threshold navigations (NAS is often this fast). */
const SKELETON_DELAY_MS = 200;
const IFD_DND = "application/x-ugreen-drive-paths";

function nameOrder(a, b) {
  return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
}

function compareVisible(a, b) {
  if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
  let order = 0;
  if (state.sortKey === "kind") {
    order = describeKind(a).label.localeCompare(describeKind(b).label);
    if (!order) order = nameOrder(a, b);
  } else if (state.sortKey === "size") {
    const sa = Number(a.size);
    const sb = Number(b.size);
    order = (Number.isFinite(sa) ? sa : -1) - (Number.isFinite(sb) ? sb : -1);
    if (!order) order = nameOrder(a, b);
  } else if (state.sortKey === "mtime") {
    order = (Date.parse(a.mtime) || 0) - (Date.parse(b.mtime) || 0);
    if (!order) order = nameOrder(a, b);
  } else {
    order = nameOrder(a, b);
  }
  return state.sortAsc ? order : -order;
}

function setSort(key) {
  if (!SORT_KEYS.has(key)) return;
  if (state.sortKey === key) {
    state.sortAsc = !state.sortAsc;
  } else {
    state.sortKey = key;
    state.sortAsc = key === "name" || key === "kind";
  }
  store(SORT_KEY, state.sortKey);
  store(SORT_DIR_KEY, state.sortAsc ? "1" : "0");
  render();
}

/** Items after search filter + folders-first sort — the order the user sees. */
function visibleItems() {
  const query = state.query.trim().toLowerCase();
  const filtered = state.items.filter((item) =>
    (!state.hideDotfiles || !item.name.startsWith("."))
    && (!query || item.name.toLowerCase().includes(query)));
  return filtered.sort(compareVisible);
}

/* ---------------------------------------------------------------------------
   Recursive search typeahead
   --------------------------------------------------------------------------- */
function searchScopePath() {
  return searchUi.scope === "all" ? "" : state.path || "";
}

function closeSearchPanel() {
  searchUi.open = false;
  searchUi.active = -1;
  const wrap = $("search-wrap");
  const panel = $("search-panel");
  const input = $("search");
  if (wrap) wrap.classList.remove("is-open");
  if (panel) {
    panel.hidden = true;
    panel.innerHTML = "";
  }
  if (input) input.setAttribute("aria-expanded", "false");
}

function clearSearch() {
  state.query = "";
  if ($("search")) $("search").value = "";
  searchUi.results = [];
  searchUi.error = null;
  searchUi.loading = false;
  searchUi.requestId += 1;
  if (searchTimer) {
    clearTimeout(searchTimer);
    searchTimer = null;
  }
  closeSearchPanel();
}

function highlightMatch(text, query) {
  const raw = text || "";
  const q = (query || "").trim();
  if (!q) return [raw];
  const lower = raw.toLowerCase();
  // Prefer longest token that appears in the name for the mark.
  const tokens = q
    .toLowerCase()
    .split(/[\s_\-./\\]+/)
    .filter((t) => t.length >= 1)
    .sort((a, b) => b.length - a.length);
  let idx = -1;
  let len = 0;
  for (const tok of tokens) {
    const at = lower.indexOf(tok);
    if (at >= 0) {
      idx = at;
      len = tok.length;
      break;
    }
  }
  if (idx < 0) {
    const at = lower.indexOf(q.toLowerCase());
    if (at >= 0) {
      idx = at;
      len = q.length;
    }
  }
  if (idx < 0) return [raw];
  return [
    raw.slice(0, idx),
    el("mark", { text: raw.slice(idx, idx + len) }),
    raw.slice(idx + len),
  ];
}

function formatSearchPath(item) {
  if (item.is_dir) {
    const parent = item.parent || "";
    return parent ? parent : "Drive root";
  }
  const parent = item.parent || "";
  return parent ? parent : "Drive root";
}

function renderSearchPanel() {
  const panel = $("search-panel");
  const wrap = $("search-wrap");
  const input = $("search");
  if (!panel || !wrap || !input) return;

  const q = state.query.trim();
  if (!q || !searchUi.open) {
    closeSearchPanel();
    return;
  }

  wrap.classList.add("is-open");
  panel.hidden = false;
  input.setAttribute("aria-expanded", "true");
  panel.innerHTML = "";

  const hereLabel = state.path ? folderLabel(state.path) : "this folder";
  const head = el("div", { class: "search-panel__head" }, [
    el("div", { class: "search-panel__scope" }, [
      el(
        "button",
        {
          type: "button",
          class: `search-panel__chip${searchUi.scope === "here" ? " is-active" : ""}`,
          text: state.path ? `In ${hereLabel}` : "Everywhere",
          onclick: (event) => {
            event.preventDefault();
            event.stopPropagation();
            if (searchUi.scope !== "here") {
              searchUi.scope = "here";
              runSearch(true);
            }
          },
        },
      ),
      state.path
        ? el(
            "button",
            {
              type: "button",
              class: `search-panel__chip${searchUi.scope === "all" ? " is-active" : ""}`,
              text: "Entire drive",
              onclick: (event) => {
                event.preventDefault();
                event.stopPropagation();
                if (searchUi.scope !== "all") {
                  searchUi.scope = "all";
                  runSearch(true);
                }
              },
            },
          )
        : null,
    ]),
    el("div", {
      class: "search-panel__meta",
      text: searchUi.loading
        ? "Searching…"
        : searchUi.results.length
          ? `${searchUi.results.length}${searchUi.truncated || searchUi.results.length >= 40 ? "+" : ""} match${searchUi.results.length === 1 ? "" : "es"}`
          : "",
    }),
  ]);
  panel.append(head);

  if (searchUi.loading && !searchUi.results.length) {
    panel.append(
      el("div", { class: "search-panel__status" }, [
        el("strong", { text: "Searching nested folders…" }),
        el("div", { class: "search-panel__hint", text: "Matching names, paths, and file kinds" }),
      ]),
    );
    return;
  }

  if (searchUi.error) {
    panel.append(
      el("div", { class: "search-panel__empty" }, [
        el("strong", { text: "Search failed" }),
        el("div", { text: searchUi.error }),
      ]),
    );
    return;
  }

  if (!searchUi.results.length) {
    panel.append(
      el("div", { class: "search-panel__empty" }, [
        el("strong", { text: `No results for “${q}”` }),
        el("div", {
          class: "search-panel__hint",
          text: searchUi.scope === "here" && state.path
            ? "Try Entire drive, or a kind like video / pdf"
            : "Try fewer words, or a kind like video / pdf / folder",
        }),
      ]),
    );
    return;
  }

  const list = el("div", { class: "search-panel__list", role: "presentation" });
  searchUi.results.forEach((item, index) => {
    const kind = describeKind(item);
    const isActive = index === searchUi.active;
    const meta = item.is_dir ? "Folder" : formatSize(item.size);
    const row = el(
      "button",
      {
        type: "button",
        class: `search-hit${isActive ? " is-active" : ""}`,
        role: "option",
        id: `search-hit-${index}`,
        "aria-selected": isActive ? "true" : "false",
        title: item.path,
        onmousedown: (event) => {
          // Prevent input blur before click opens the result.
          event.preventDefault();
        },
        onclick: (event) => {
          event.preventDefault();
          event.stopPropagation();
          void openSearchResult(item, event.metaKey || event.ctrlKey || event.altKey);
        },
      },
      [
        el("div", { class: "search-hit__icon", style: `color:${kind.color}` }, [icon(kind.icon, 16)]),
        el("div", { class: "search-hit__body" }, [
          el("div", { class: "search-hit__name" }, highlightMatch(displayName(item.name), q)),
          el("div", { class: "search-hit__path", text: formatSearchPath(item) }),
        ]),
        el("div", { class: "search-hit__meta", text: meta }),
      ],
    );
    list.append(row);
  });
  panel.append(list);

  panel.append(
    el("div", { class: "search-panel__foot" }, [
      el("span", {
        text: searchUi.truncated
          ? "Partial scan — refine your query for deeper hits"
          : "Enter open · ⌥Enter show in folder · Esc close",
      }),
      el("span", {}, [
        el("kbd", { text: "↑" }),
        " ",
        el("kbd", { text: "↓" }),
      ]),
    ]),
  );

  if (searchUi.active >= 0) {
    input.setAttribute("aria-activedescendant", `search-hit-${searchUi.active}`);
    const activeEl = panel.querySelector(".search-hit.is-active");
    activeEl?.scrollIntoView({ block: "nearest" });
  } else {
    input.removeAttribute("aria-activedescendant");
  }
}

async function runSearch(immediate = false) {
  const q = state.query.trim();
  if (!q) {
    searchUi.results = [];
    searchUi.error = null;
    searchUi.loading = false;
    closeSearchPanel();
    return;
  }

  searchUi.open = true;
  const requestId = ++searchUi.requestId;
  const delay = immediate ? 0 : 160;

  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    searchTimer = null;
    if (requestId !== searchUi.requestId) return;
    searchUi.loading = true;
    searchUi.error = null;
    renderSearchPanel();
    try {
      const data = await api.searchFiles(q, searchScopePath(), 40);
      if (requestId !== searchUi.requestId) return;
      searchUi.results = data.results || [];
      searchUi.truncated = Boolean(data.truncated || data.has_more);
      searchUi.scanned = data.scanned || 0;
      searchUi.loading = false;
      searchUi.error = null;
      if (searchUi.active >= searchUi.results.length) searchUi.active = searchUi.results.length - 1;
      if (searchUi.active < 0 && searchUi.results.length) searchUi.active = 0;
      renderSearchPanel();
    } catch (error) {
      if (requestId !== searchUi.requestId) return;
      searchUi.loading = false;
      searchUi.results = [];
      searchUi.error = error.message || "Search failed";
      renderSearchPanel();
    }
  }, delay);
}

async function openSearchResult(item, revealOnly = false) {
  if (!item) return;
  const parent =
    item.parent != null
      ? item.parent
      : item.path.includes("/")
        ? item.path.slice(0, item.path.lastIndexOf("/"))
        : "";

  closeSearchPanel();

  if (revealOnly) {
    // Keep query so the destination list stays filtered to this name.
    navigate(item.is_dir ? item.path : parent);
    const targetPath = item.path;
    const destPath = item.is_dir ? item.path : parent;
    const startedAt = loadRequestId;
    const finish = () => {
      if (state.loading) return false;
      if (state.path !== destPath && loadRequestId === startedAt) return false;
      state.selection = new Set([targetPath]);
      render();
      return true;
    };
    if (!finish()) {
      const onDone = () => {
        if (finish()) window.removeEventListener("hashchange", onDone);
      };
      // Folder load is async; poll briefly after navigation.
      let tries = 0;
      const poll = () => {
        if (finish() || tries++ > 40) return;
        setTimeout(poll, 50);
      };
      setTimeout(poll, 50);
      window.addEventListener("hashchange", onDone, { once: true });
    }
    return;
  }

  state.query = "";
  if ($("search")) $("search").value = "";

  if (item.is_dir) {
    navigate(item.path);
    return;
  }

  // Preview/download works from path without needing the parent listing.
  if (previewKind(item)) {
    openPreview(item);
    return;
  }
  downloadItems([item]);
}

function moveSearchActive(delta) {
  if (!searchUi.open || !searchUi.results.length) return;
  const n = searchUi.results.length;
  if (searchUi.active < 0) searchUi.active = delta > 0 ? 0 : n - 1;
  else searchUi.active = (searchUi.active + delta + n) % n;
  renderSearchPanel();
}

function selectedItems() {
  return state.items.filter((item) => state.selection.has(item.path));
}

function itemRelPath(item) {
  return item?.path || item?.name || "";
}

async function copyItemPaths(items) {
  const lines = (items || []).map(itemRelPath).filter(Boolean);
  if (!lines.length) {
    const fallback = state.path || shareRootLabel();
    lines.push(fallback);
  }
  const text = lines.join("\n");
  try {
    await navigator.clipboard.writeText(text);
    toast(lines.length === 1 ? "Copied path" : `Copied ${lines.length} paths`);
  } catch {
    toast("Could not copy path", "warn");
  }
}

async function copyItemLinks(items) {
  if (!items.length) return;
  const base = state.me?.public_base_url || location.origin;
  const links = items.map((item) => {
    // Path in the query, not the hash, so iMessage/Slack previews can show the item.
    const url = new URL("/open", base);
    url.search = new URLSearchParams({ share: state.share, [item.is_dir ? "path" : "file"]: item.path });
    return url.href;
  });
  const text = links.join("\n");
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch {
    // Plain-HTTP LAN connections may not expose navigator.clipboard.
    const field = document.createElement("textarea");
    const previousFocus = document.activeElement;
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    try {
      field.focus();
      field.select();
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    } finally {
      field.remove();
      previousFocus?.focus({ preventScroll: true });
    }
  }
  if (copied) {
    toast(links.length === 1 ? "Link copied — Drive access required" : "Links copied — Drive access required");
  } else {
    window.prompt("Copy the link below. Recipients need Drive access:", text);
  }
}

function hasTextSelection() {
  const sel = window.getSelection();
  return Boolean(sel && String(sel).trim());
}

/* ---------------------------------------------------------------------------
   Toast
   --------------------------------------------------------------------------- */
function toast(message, kind = "ok", opts = {}) {
  const node = $("toast");
  $("toast-text").textContent = message;
  $("toast-icon").setAttribute("href", kind === "ok" ? "#i-check" : "#i-alert");
  node.classList.toggle("is-error", kind !== "ok");
  const action = $("toast-action");
  const hasAction = Boolean(opts.actionLabel && typeof opts.onAction === "function");
  if (hasAction) {
    action.textContent = opts.actionLabel;
    action.onclick = () => {
      show(node, false);
      clearTimeout(toastTimer);
      opts.onAction();
    };
  } else {
    action.onclick = null;
  }
  show(action, hasAction);
  show(node, true);
  clearTimeout(toastTimer);
  const duration = opts.duration ?? (hasAction ? 7000 : 3200);
  toastTimer = setTimeout(() => show(node, false), duration);
}

/* ---------------------------------------------------------------------------
   Theme — keep html class, data-theme, color-scheme, and meta in lockstep.
   Partial switches on reload came from FOUC + only toggling .light late.
   --------------------------------------------------------------------------- */
function applyTheme(theme) {
  const light = theme === "light";
  const root = document.documentElement;
  root.classList.toggle("light", light);
  root.dataset.theme = light ? "light" : "dark";
  root.style.colorScheme = light ? "light" : "dark";

  const schemeMeta = document.getElementById("meta-color-scheme");
  const colorMeta = document.getElementById("meta-theme-color");
  if (schemeMeta) schemeMeta.setAttribute("content", light ? "light" : "dark");
  if (colorMeta) colorMeta.setAttribute("content", light ? "#f4f6f5" : "#0f110f");

  const icon = $("theme-icon");
  if (icon) icon.setAttribute("href", light ? "#i-moon" : "#i-sun");

  store(THEME_KEY, light ? "light" : "dark");
}

function storedTheme() {
  return readStored(THEME_KEY, "dark") === "light" ? "light" : "dark";
}

/* ---------------------------------------------------------------------------
   Routing — the folder path lives in the URL hash so Back/Forward work
   --------------------------------------------------------------------------- */
function pathFromHash() {
  const raw = location.hash.split("?")[0].replace(/^#\/?/, "");
  if (!raw) return "";
  return raw
    .split("/")
    .filter(Boolean)
    .map((segment) => {
      try {
        return decodeURIComponent(segment);
      } catch {
        return segment;
      }
    })
    .join("/");
}

function hashForPath(path) {
  const parts = pathParts(path).map(encodeURIComponent);
  return parts.length ? `#/${parts.join("/")}` : "#/";
}

/** Hash route for a copied `/open?share=…&path=…|file=…` link. */
function openLinkHash(search) {
  const query = new URLSearchParams(search);
  const file = query.get("file") || "";
  const params = new URLSearchParams();
  if (query.get("share")) params.set("share", query.get("share"));
  if (file) params.set("file", file);
  const hash = hashForPath(file ? parentPath(file) : query.get("path") || "");
  const tail = params.toString();
  return tail ? `${hash}?${tail}` : hash;
}

async function loadRoute() {
  if (!state.me?.authenticated) {
    showLogin();
    return;
  }
  const hash = location.hash;
  const params = new URLSearchParams(hash.split("?")[1] || "");
  const share = params.get("share");
  // Invalidate an in-flight listing before changing its share.
  ++loadRequestId;
  try {
    if (share && !state.shares.some((entry) => entry.id === share)) {
      throw new ApiError("You don't have access to this drive.", 403);
    }
    if (share && share !== state.share) {
      const result = await api.setShare(share);
      if (location.hash !== hash) return;
      if (result.share !== share) {
        throw new ApiError("You don't have access to this drive.", 403);
      }
      state.share = share;
      api.setActiveShare(share);
      store(SHARE_KEY, share);
      setQuickScope(state.me.nas_username, share);
      state.treeChildren = Object.create(null);
      state.treeExpanded = new Set();
      state.items = [];
      clearSearch();
      loadUsage();
      refreshShortcuts();
    }
  } catch (error) {
    if (location.hash !== hash) return;
    state.path = pathFromHash();
    state.items = [];
    state.selection.clear();
    state.loading = false;
    state.showSkeleton = false;
    state.error = error;
    if (error instanceof ApiError && [423, 428].includes(error.status) && state.share.startsWith("~")) {
      openPersonalUnlock(state.share, error.status === 428);
    }
    render();
    return;
  }
  await loadFolder(pathFromHash());
  if (location.hash !== hash || state.error || state.loading) return;
  const file = params.get("file");
  if (!file) return;
  const item = state.items.find((entry) => entry.path === file && !entry.is_dir);
  if (!item) {
    toast("File not found or you don't have access to it", "warn");
    return;
  }
  clearSearch();
  state.selection = new Set([item.path]);
  state.cursorIndex = visibleItems().findIndex((entry) => entry.path === file);
  state.anchorIndex = state.cursorIndex;
  render();
  scrollItemIntoView(item);
  if (previewKind(item)) openPreview(item);
}

function navigate(path) {
  closeSearchPanel();
  const next = hashForPath(path);
  if (location.hash === next) {
    loadFolder(path);
  } else {
    location.hash = next;
  }
}

/* ---------------------------------------------------------------------------
   Data loading
   --------------------------------------------------------------------------- */
async function loadFolder(path) {
  const requestId = ++loadRequestId;
  state.path = path;
  state.loading = true;
  state.showSkeleton = false;
  state.error = null;
  state.selection.clear();
  state.anchorIndex = null;
  state.cursorIndex = null;
  state.openMenu = null;

  // Keep the previous listing on screen for fast loads. Only reveal the
  // skeleton if the NAS still hasn't answered after SKELETON_DELAY_MS.
  if (skeletonTimer) clearTimeout(skeletonTimer);
  skeletonTimer = setTimeout(() => {
    if (state.loading && requestId === loadRequestId) {
      state.showSkeleton = true;
      render();
    }
  }, SKELETON_DELAY_MS);

  render();

  try {
    const data = await api.listFiles(path);
    if (requestId !== loadRequestId) return;
    state.path = data.path || "";
    state.items = data.items || [];
    state.hasRecycle = Boolean(data.has_recycle);
    state.loading = false;
    state.showSkeleton = false;
    // Keep sidebar tree in sync with the open folder.
    state.treeChildren[state.path] = state.items.filter((item) => item.is_dir);
    // Expand path ancestors; collapse anything no longer on the path so
    // Up / browser-back doesn't leave a trail of open folders.
    syncTreeExpanded(state.path);
    if (state.path && !isUnderRecycle(state.path)) pushRecent(state.path);
    warmFolderThumbs();
  } catch (error) {
    if (requestId !== loadRequestId) return;
    if (error instanceof ApiError && error.isAuth) {
      showLogin();
      return;
    }
    if (error instanceof ApiError && error.status === 404) {
      // Folder is gone — drop it from Favorites/Recent so it can't be revisited.
      removePath(path);
      toast("Folder no longer exists — removed from quick access", "warn");
    }
    state.loading = false;
    state.showSkeleton = false;
    state.items = [];
    state.error = error;
    if (error instanceof ApiError && [423, 428].includes(error.status) && state.share.startsWith("~")) {
      openPersonalUnlock(state.share, error.status === 428);
    }
  } finally {
    if (requestId === loadRequestId && skeletonTimer) {
      clearTimeout(skeletonTimer);
      skeletonTimer = null;
    }
  }
  if (requestId !== loadRequestId) return;
  render();
  renderTree();
  void hydrateFolderSizes(requestId);
  void hydrateTreeAncestors(state.path);
  if (!state.path) refreshShortcuts();
}

/** Human size for list/grid; folders load recursively after the listing. */
function itemSizeLabel(item) {
  if (!item) return "—";
  if (!item.is_dir) return formatSize(item.size);
  if (!item._sizeLoaded) return "…";
  const text = formatSize(item.size ?? 0);
  return item.size_incomplete ? `~${text}` : text;
}

/**
 * After a folder list renders, fetch recursive content sizes for each subfolder
 * and patch the SIZE column (and grid meta) without a full re-render.
 */
async function hydrateFolderSizes(requestId) {
  const applySizes = (sizes) => {
    let changed = false;
    for (const item of state.items) {
      if (!item.is_dir || item._sizeLoaded) continue;
      const hit = sizes[item.path];
      if (!hit) continue;
      item.size = Number(hit.size) || 0;
      item.size_incomplete = Boolean(hit.incomplete);
      item._sizeLoaded = true;
      changed = true;
    }
    if (changed) patchFolderSizeDom();
  };

  const pendingPaths = () =>
    (state.items || []).filter((item) => item.is_dir && !item._sizeLoaded).map((item) => item.path);

  // Up to two passes: first fills cache-warm paths; second finishes any that
  // the server skipped when the shared time budget ran out.
  for (let pass = 0; pass < 2; pass += 1) {
    const paths = pendingPaths();
    if (!paths.length) return;
    if (requestId !== loadRequestId) return;

    // Chunk so one huge listing doesn't create a giant query string / walk.
    const CHUNK = 24;
    for (let i = 0; i < paths.length; i += CHUNK) {
      if (requestId !== loadRequestId) return;
      const chunk = paths.slice(i, i + CHUNK);
      let data;
      try {
        data = await api.folderSizes(chunk);
      } catch {
        // Leave "…" — list is still usable without sizes.
        continue;
      }
      if (requestId !== loadRequestId) return;
      applySizes(data?.sizes || {});
    }
  }
}

function patchFolderSizeDom() {
  for (const item of state.items || []) {
    if (!item.is_dir || !item._sizeLoaded) continue;
    const label = itemSizeLabel(item);
    const rowSize = document.querySelector(`.row[data-path="${cssPath(item.path)}"] .row__size`);
    if (rowSize) {
      rowSize.textContent = label;
      if (item.size_incomplete) rowSize.title = "Approximate — folder is large or still scanning";
      else rowSize.removeAttribute("title");
    }
    const tileMeta = document.querySelector(`.tile[data-path="${cssPath(item.path)}"] .tile__meta`);
    if (tileMeta) tileMeta.textContent = label;
  }
}

/** Escape a path for use inside a CSS attribute selector. */
function cssPath(path) {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
    return CSS.escape(path || "");
  }
  return String(path || "").replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

async function refreshShortcuts() {
  try {
    const data = await api.listFiles("");
    state.shortcuts = (data.items || []).filter((item) => item.is_dir);
    state.treeChildren[""] = state.shortcuts;
  } catch {
    state.shortcuts = [];
    state.treeChildren[""] = [];
  }
  renderTree();
}

/* ---------------------------------------------------------------------------
   Sidebar folder tree
   --------------------------------------------------------------------------- */
function expandAncestors(path) {
  const parts = pathParts(path);
  let acc = "";
  for (const part of parts) {
    acc = acc ? `${acc}/${part}` : part;
    state.treeExpanded.add(acc);
  }
}

/** True if `nodePath` is the current folder or an ancestor of it. */
function isTreePathOnRoute(nodePath, currentPath) {
  if (!nodePath) return true;
  if (!currentPath) return false;
  return currentPath === nodePath || currentPath.startsWith(`${nodePath}/`);
}

/**
 * Expand every ancestor of `path` (and `path` itself); collapse any other
 * open tree nodes. Keeps the sidebar tidy when navigating up or back.
 */
function syncTreeExpanded(path) {
  for (const open of [...state.treeExpanded]) {
    if (!isTreePathOnRoute(open, path)) state.treeExpanded.delete(open);
  }
  expandAncestors(path);
}

async function ensureTreeChildren(path) {
  if (Object.prototype.hasOwnProperty.call(state.treeChildren, path)) return;
  state.treeLoading.add(path);
  renderTree();
  try {
    const data = await api.listFiles(path);
    state.treeChildren[path] = (data.items || []).filter((item) => item.is_dir);
  } catch {
    state.treeChildren[path] = [];
  }
  state.treeLoading.delete(path);
  renderTree();
}

async function hydrateTreeAncestors(path) {
  const parts = pathParts(path);
  let parent = "";
  for (let i = 0; i < parts.length; i++) {
    if (!Object.prototype.hasOwnProperty.call(state.treeChildren, parent)) {
      await ensureTreeChildren(parent);
    }
    parent = parent ? `${parent}/${parts[i]}` : parts[i];
  }
  // Ensure current folder is expanded so nested children show after open.
  if (path) state.treeExpanded.add(path);
  renderTree();
}

async function toggleTreeExpand(path) {
  if (state.treeExpanded.has(path)) {
    state.treeExpanded.delete(path);
    renderTree();
    return;
  }
  state.treeExpanded.add(path);
  renderTree();
  await ensureTreeChildren(path);
}

let personalRelockTimer = null;

async function loadUsage() {
  const share = state.share;
  try {
    const usage = await api.getUsage();
    if (share !== state.share) return;
    if (personalRelockTimer) clearTimeout(personalRelockTimer);
    personalRelockTimer = null;
    if (usage?.expires_at) {
      personalRelockTimer = setTimeout(() => {
        if (state.share !== share) return;
        closeModal();
        state.items = [];
        state.treeChildren = Object.create(null);
        void loadFolder(state.path);
        void loadUsage();
      }, Math.max(1000, usage.expires_at * 1000 - Date.now() + 1000));
    }
    if (!usage || !usage.total) return;
    const personalQuota = usage.scope === "personal";
    const used = Number(usage.used) || 0;
    const total = Number(usage.total) || 0;
    const free = usage.free != null ? Number(usage.free) : Math.max(0, total - used);
    const percent = total > 0 ? Math.min(100, Math.max(0, (used / total) * 100)) : 0;

    // e.g. "596 GB · 5.5 TB free" — free is what matters for uploads
    const compact = `${formatSize(used)} / ${formatSize(total)}`;
    const withFree = `${formatSize(used)} used · ${formatSize(free)} free`;
    let tip = personalQuota
      ? "Space available within your UGOS personal-folder storage limit."
      : "Space on the NAS volume that holds this folder. Other folders on the same volume also count toward used.";
    if (usage.expires_at) tip += ` Relocks everywhere at ${new Date(usage.expires_at * 1000).toLocaleString()}.`;

    const text = $("storage-text");
    text.textContent = compact;
    text.title = `${withFree} of ${formatSize(total)} total.\n${tip}`;
    $("storage-bar").style.width = `${percent}%`;
    $("storage-bar").parentElement?.setAttribute("title", tip);

    const labelEl = $("storage-label");
    if (labelEl) {
      labelEl.textContent = personalQuota ? "Storage" : "NAS volume";
      labelEl.title = tip;
    }

    $("login-storage").textContent = withFree;
    $("login-storage").title = tip;
    show($("storage-block"), true);
  } catch {
    show($("storage-block"), false);
  }
}

/* ---------------------------------------------------------------------------
   Screens
   --------------------------------------------------------------------------- */
/** Show a renamed drive without a reload (wordmarks + tab title). */
function applyDriveName(name) {
  if (!name) return;
  for (const node of document.querySelectorAll("[data-drive-name]")) node.textContent = name;
  document.title = name;
}

function hideBootSplash() {
  const boot = $("screen-boot");
  if (!boot || boot.hidden) return;
  boot.setAttribute("aria-busy", "false");
  boot.classList.add("is-done");
  // Remove after fade so it never intercepts clicks / live-region noise.
  const finish = () => {
    boot.hidden = true;
    boot.classList.remove("is-done");
  };
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    finish();
    return;
  }
  window.setTimeout(finish, 200);
}

function showLogin() {
  for (const link of document.querySelectorAll('a[href^="/auth/login"]')) {
    link.href = `/auth/login?next=${encodeURIComponent(`/${location.hash || "#/"}`)}`;
  }
  const params = new URLSearchParams(location.search);
  const denied = params.get("error") === "not_authorized";
  const email = params.get("email") || "";

  show($("screen-browser"), false);
  show($("screen-login"), true);
  show($("login-signin"), !denied);
  show($("login-denied"), denied);
  const methods = state.me?.sign_in || {};
  show($("login-google"), Boolean(methods.google));
  show($("login-email"), Boolean(methods.email));
  const hostLabel = $("login-host");
  if (hostLabel) hostLabel.textContent = location.host;
  hideBootSplash();

  if (denied) {
    const detail = $("denied-detail");
    detail.textContent = "";
    if (email) {
      detail.append("You signed in as ", el("code", { text: email }), ", but that email isn't linked to a NAS account.");
    } else {
      detail.textContent = "Your Google account signed in, but it isn't linked to a NAS account.";
    }
  }
  bindEmailSignIn();
  bindNasLogin();
}

function bindNasLogin() {
  const form = $("nas-form");
  if (!form || form.dataset.bound === "1") return;
  form.dataset.bound = "1";
  const err = $("nas-error");
  const otpWrap = $("nas-otp-wrap");
  const passWrap = $("nas-password-wrap");
  const submit = $("nas-submit");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (err) {
      err.hidden = true;
      err.textContent = "";
    }
    const username = ($("nas-username")?.value || "").trim();
    const password = $("nas-password")?.value || "";
    const otp = ($("nas-otp")?.value || "").trim();
    if (submit) submit.disabled = true;
    try {
      if (otpWrap && !otpWrap.hidden) {
        await api.nasOtp(otp);
      } else {
        const out = await api.nasLogin(username, password);
        if (out && out.need_otp) {
          if (passWrap) passWrap.hidden = true;
          const pass = $("nas-password");
          if (pass) pass.required = false;
          if (otpWrap) otpWrap.hidden = false;
          const otpField = $("nas-otp");
          if (otpField) otpField.required = true;
          if (submit) submit.textContent = "Verify";
          otpField?.focus();
          return;
        }
      }
      location.reload();
    } catch (ex) {
      if (err) {
        err.hidden = false;
        err.textContent = ex instanceof ApiError ? ex.message : "Sign-in failed.";
      }
    } finally {
      if (submit) submit.disabled = false;
    }
  });
}

function showBrowser() {
  show($("screen-login"), false);
  show($("screen-browser"), true);
  hideBootSplash();

  const me = state.me;
  state.isAdmin = Boolean(me.is_admin);
  state.shares = Array.isArray(me.shares) && me.shares.length
    ? me.shares
    : [{ id: "", name: "No shared folders" }];
  // Prefer localStorage (survives sticky session wipes), then server session, then default.
  const allowed = new Set(state.shares.map((s) => s.id));
  const stored = readStored(SHARE_KEY, "");
  const preferred =
    (allowed.has(stored) ? stored : "") || me.share || state.shares[0].id;
  state.share = allowed.has(preferred) ? preferred : state.shares[0].id;
  api.setActiveShare(state.share);
  store(SHARE_KEY, state.share);

  $("user-name").textContent = me.name || me.email || "";
  $("user-nas").textContent = me.nas_username || "";

  const avatar = $("user-avatar");
  avatar.textContent = "";
  if (me.picture) {
    avatar.append(el("img", { src: me.picture, alt: "", width: 30, height: 30, style: "width:100%;height:100%;object-fit:cover;border-radius:50%" }));
  } else {
    const source = me.name || me.nas_username || me.email || "?";
    avatar.textContent = source
      .split(/[\s._-]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((word) => word[0].toUpperCase())
      .join("");
  }

  if (me.ugos_admin_path) {
    $("admin-link").href = me.ugos_admin_path;
    show($("admin-link"), true);
  }
  show($("btn-settings"), state.isAdmin);
}

/* ---------------------------------------------------------------------------
   Rendering
   --------------------------------------------------------------------------- */
function render() {
  renderCrumbs();
  updateTreeActive();
  renderActionBar();
  renderContent();
  renderQuickAccess();
}

/** One Favorites/Recent sidebar row: navigate on click, optional remove ×. */
function quickRow(path, iconId, onRemove) {
  const label = baseName(path) || "Drive root";
  const row = el(
    "div",
    {
      class: "tree-row quick-row",
      role: "link",
      tabindex: "0",
      "data-path": path,
      title: path,
      onclick: () => navigate(path),
      onkeydown: (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          navigate(path);
        }
      },
    },
    [
      icon(iconId, 14, "flex:0 0 auto"),
      el("span", { class: "tree-row__label", text: label }),
    ],
  );
  if (onRemove) {
    row.append(
      el(
        "button",
        {
          type: "button",
          class: "quick-row__remove",
          title: "Remove from favorites",
          "aria-label": `Remove ${label} from favorites`,
          onclick: (event) => {
            event.stopPropagation();
            onRemove();
          },
        },
        [icon("#i-x", 11)],
      ),
    );
  }
  return row;
}

function renderQuickAccess() {
  const favBox = $("quick-favorites");
  const recBox = $("quick-recents");
  if (!favBox || !recBox) return;
  const favs = listFavorites();
  const recents = listRecents().filter((path) => !favs.includes(path));
  favBox.textContent = "";
  recBox.textContent = "";
  show($("quick-fav-label"), favs.length > 0);
  show($("quick-rec-label"), recents.length > 0);
  for (const path of favs) {
    favBox.append(
      quickRow(path, "#i-star", () => {
        toggleFavorite(path);
        renderQuickAccess();
      }),
    );
  }
  // Keep the sidebar tight — history still holds more, we just surface a few.
  for (const path of recents.slice(0, 2)) {
    recBox.append(quickRow(path, "#i-clock", null));
  }
}

function renderTree() {
  const nav = $("shortcuts");
  if (!nav) return;
  nav.textContent = "";

  const roots = state.treeChildren[""] || state.shortcuts || [];
  if (!roots.length) {
    nav.append(
      el("p", {
        class: "sidebar__empty",
        text: "No folders at the drive root, or your NAS account can't read it.",
      }),
    );
    return;
  }

  for (const item of roots) {
    nav.append(buildTreeNode(item, 0));
  }
  updateTreeActive();
}

function buildTreeNode(item, depth) {
  const expanded = state.treeExpanded.has(item.path);
  const loading = state.treeLoading.has(item.path);
  const known = Object.prototype.hasOwnProperty.call(state.treeChildren, item.path);
  const kids = known ? state.treeChildren[item.path] : null;
  const emptyKnown = known && kids.length === 0;

  const node = el("div", { class: "tree-node" });

  const chev = el(
    "button",
    {
      type: "button",
      class: `tree-chev${expanded ? " is-open" : ""}${emptyKnown ? " is-empty" : ""}${loading ? " is-loading" : ""}`,
      "aria-label": expanded ? `Collapse ${item.name}` : `Expand ${item.name}`,
      "aria-expanded": String(expanded),
      tabindex: emptyKnown ? "-1" : "0",
      onclick: (event) => {
        event.stopPropagation();
        if (emptyKnown) return;
        void toggleTreeExpand(item.path);
      },
    },
    [icon("#i-chev-r", 12)],
  );

  const label = displayName(item.name);
  const recycle = isRecycleName(item.name) || item.path === "#recycle";
  const folderIcon = icon(recycle ? "#i-recycle" : "#i-folder", 15);
  folderIcon.classList.add("tree-row__icon");
  if (recycle) folderIcon.classList.add("tree-row__icon--recycle");

  const row = el(
    "div",
    {
      class: "tree-row",
      role: "button",
      tabindex: "0",
      "data-path": item.path,
      style: `--depth:${depth}`,
      title: label,
      onclick: () => {
        if (state.suppressClick) return;
        closeDrawer();
        navigate(item.path);
      },
      onkeydown: (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          closeDrawer();
          navigate(item.path);
        } else if (event.key === "ArrowRight" && !expanded && !emptyKnown) {
          event.preventDefault();
          void toggleTreeExpand(item.path);
        } else if (event.key === "ArrowLeft" && expanded) {
          event.preventDefault();
          void toggleTreeExpand(item.path);
        }
      },
    },
    [chev, folderIcon, el("span", { class: "tree-row__label", text: label })],
  );
  bindItemDrag(row, item);
  bindFolderDrop(row, item.path);

  node.append(row);

  if (expanded) {
    const kidsWrap = el("div", { class: "tree-kids" });
    if (loading && !known) {
      kidsWrap.append(el("div", { class: "tree-empty", style: `--depth:${depth + 1}`, text: "Loading…" }));
    } else if (known && kids.length === 0) {
      kidsWrap.append(el("div", { class: "tree-empty", style: `--depth:${depth + 1}`, text: "No subfolders" }));
    } else if (kids) {
      for (const child of kids) {
        kidsWrap.append(buildTreeNode(child, depth + 1));
      }
    }
    node.append(kidsWrap);
  }

  return node;
}

function updateTreeActive() {
  const nav = $("shortcuts");
  if (!nav) return;
  for (const row of nav.querySelectorAll(".tree-row")) {
    const p = row.dataset.path || "";
    const current = p === state.path;
    const onPath = !current && p && (state.path === p || state.path.startsWith(`${p}/`));
    row.classList.toggle("tree-row--current", current);
    row.classList.toggle("tree-row--onpath", onPath);
    row.setAttribute("aria-current", current ? "true" : "false");
  }
}

function shareRootLabel() {
  const id = state.share || "";
  const match = (state.shares || []).find((s) => s.id === id);
  return (match && match.name) || id;
}

function closeShareMenu() {
  state.shareMenuOpen = false;
  for (const menu of document.querySelectorAll(".share-switch__menu")) {
    menu.remove();
  }
  for (const btn of document.querySelectorAll(".share-switch__btn")) {
    btn.setAttribute("aria-expanded", "false");
  }
  window.removeEventListener("resize", closeShareMenu);
  window.removeEventListener("scroll", closeShareMenu, true);
}

function closeCrumbOverflow() {
  const menu = document.getElementById("crumb-overflow-menu");
  if (menu) menu.remove();
  for (const btn of document.querySelectorAll(".crumb--more")) {
    btn.setAttribute("aria-expanded", "false");
  }
}

function toggleCrumbOverflow(anchor, hidden) {
  const wasOpen = anchor.getAttribute("aria-expanded") === "true";
  closeCrumbOverflow();
  if (wasOpen) return;
  const menu = el("div", {
    class: "menu menu--portal",
    id: "crumb-overflow-menu",
    role: "menu",
    onclick: (event) => event.stopPropagation(),
  });
  for (const crumb of hidden) {
    menu.append(
      el(
        "button",
        {
          type: "button",
          role: "menuitem",
          onclick: () => {
            closeCrumbOverflow();
            navigate(crumb.path);
          },
        },
        [icon("#i-folder", 14), displayName(crumb.name)],
      ),
    );
  }
  document.body.append(menu);
  anchor.setAttribute("aria-expanded", "true");
  const rect = anchor.getBoundingClientRect();
  placeFixedMenu(menu, rect.bottom + 4, rect.left);
}

function shareOptionEl(s) {
  const active = s.id === state.share;
  const ro = s.can_write === false;
  const personal = s.kind === "personal";
  return el(
    "button",
    {
      type: "button",
      role: "option",
      "aria-selected": String(active),
      class: active ? "share-switch__option is-active" : "share-switch__option",
      onclick: (event) => {
        event.stopPropagation();
        closeShareMenu();
        void switchShare(s.id);
      },
    },
    [
      icon(s.locked ? "#i-lock" : personal ? "#i-user" : "#i-hdd", 15),
      el("span", { class: "share-switch__option-label", text: s.name }),
      s.locked
        ? el("span", { class: "share-switch__ro", text: "locked" })
        : ro
        ? el("span", { class: "share-switch__ro", text: "view", title: "Read-only for your account" })
        : null,
      active ? icon("#i-check", 14) : null,
    ],
  );
}

function toggleShareMenu(wrap, btn) {
  const wasOpen = btn.getAttribute("aria-expanded") === "true";
  closeShareMenu();
  if (wasOpen) return;

  state.shareMenuOpen = true;
  btn.setAttribute("aria-expanded", "true");
  const menu = el("div", {
    class: "menu share-switch__menu",
    role: "listbox",
    "aria-label": "Drives",
  });
  const shared = (state.shares || []).filter((s) => s.kind !== "personal");
  const personal = (state.shares || []).filter((s) => s.kind === "personal");
  const grouped = shared.length > 0 && personal.length > 0;

  const appendGroup = (label, items) => {
    if (!items.length) return;
    if (grouped) {
      menu.append(el("div", { class: "share-switch__heading", text: label }));
    }
    for (const s of items) menu.append(shareOptionEl(s));
  };

  appendGroup("Shared", shared);
  if (grouped) menu.append(el("div", { class: "menu__sep" }));
  appendGroup("Personal", personal);
  // Portal to body + fixed position so sticky topbar / crumbs overflow
  // cannot clip the list (was painting under the action bar).
  document.body.append(menu);
  const rect = btn.getBoundingClientRect();
  const menuWidth = Math.max(220, Math.min(320, rect.width + 80));
  let left = rect.left;
  if (left + menuWidth > window.innerWidth - 12) {
    left = Math.max(12, window.innerWidth - menuWidth - 12);
  }
  menu.style.position = "fixed";
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(rect.bottom + 6)}px`;
  menu.style.minWidth = `${Math.round(menuWidth)}px`;
  menu.style.right = "auto";
  menu.style.zIndex = "200";

  window.addEventListener("resize", closeShareMenu);
  window.addEventListener("scroll", closeShareMenu, true);
}

async function switchShare(shareId) {
  if (state.shares?.find((s) => s.id === shareId)?.locked) {
    closeShareMenu();
    openPersonalUnlock(shareId, Boolean(state.shares.find((s) => s.id === shareId)?.needs_owner_signin));
    return;
  }
  if (!shareId || shareId === state.share) {
    closeShareMenu();
    return;
  }
  closeShareMenu();
  try {
    // Pin client header first so concurrent list calls use the new root.
    api.setActiveShare(shareId);
    const res = await api.setShare(shareId);
    state.share = res.share || shareId;
    state.shares = res.shares || state.shares;
    api.setActiveShare(state.share);
    store(SHARE_KEY, state.share);
    setQuickScope(state.me?.nas_username, state.share);

    state.treeChildren = Object.create(null);
    state.treeExpanded = new Set();
    state.selection.clear();
    state.items = [];
    state.error = null;
    clearSearch();

    // Always reload root of the new share (hash short-circuit would skip navigate).
    if (location.hash !== "#/" && location.hash !== "") {
      location.hash = "#/";
    }
    await loadFolder("");
    await Promise.all([refreshShortcuts(), loadUsage()]);
  } catch (error) {
    // Roll back header if switch failed.
    api.setActiveShare(state.share);
    toast(error.message || "Could not switch share", "err");
  }
}

function renderCrumbs() {
  const nav = $("crumbs");
  nav.textContent = "";

  const parts = pathParts(state.path);
  const rootName = shareRootLabel();
  const atRoot = !parts.length;
  // Share picker only at the drive root (and only when 2+ shares exist).
  // Deeper in a tree, the root crumb is just "go home" — not a selector.
  const canSwitchShare = atRoot && (state.shares || []).length > 1;

  if (canSwitchShare) {
    const wrap = el("div", { class: "share-switch share-switch--current" });
    const btn = el("button", {
      type: "button",
      class: "crumb crumb--current share-switch__btn",
      title: "Switch drive",
      "aria-label": "Drive",
      "aria-haspopup": "listbox",
      "aria-expanded": "false",
      onclick: (event) => {
        if (state.suppressClick) return;
        event.stopPropagation();
        toggleShareMenu(wrap, btn);
      },
    });
    btn.append(document.createTextNode(rootName));
    btn.append(icon("#i-chev-d", 14));
    wrap.append(btn);
    bindFolderDrop(wrap, "");
    nav.append(wrap);
  } else {
    const rootBtn = el("button", {
      type: "button",
      class: atRoot ? "crumb crumb--current" : "crumb",
      text: rootName,
      title: atRoot ? rootName : `Go to ${rootName}`,
      onclick: atRoot
        ? null
        : () => {
            if (state.suppressClick) return;
            navigate("");
          },
    });
    bindFolderDrop(rootBtn, "");
    nav.append(rootBtn);
  }

  let hidden = [];
  let shown = parts;
  if (parts.length > 3) {
    hidden = parts.slice(0, -2).map((name, index) => ({
      name,
      path: parts.slice(0, index + 1).join("/"),
    }));
    shown = parts.slice(-2);
  }

  if (hidden.length) {
    nav.append(icon("#i-chev-r", 14));
    const more = el("button", {
      type: "button",
      class: "crumb crumb--more",
      text: "…",
      title: "More folders",
      "aria-label": "More folders",
      "aria-haspopup": "menu",
      "aria-expanded": "false",
      onclick: (event) => {
        if (state.suppressClick) return;
        event.stopPropagation();
        toggleCrumbOverflow(more, hidden);
      },
    });
    nav.append(more);
  }

  shown.forEach((name, shownIndex) => {
    nav.append(icon("#i-chev-r", 14));
    const index = parts.length - shown.length + shownIndex;
    const path = parts.slice(0, index + 1).join("/");
    const isLast = index === parts.length - 1;
    const crumbBtn = el("button", {
      type: "button",
      class: isLast ? "crumb crumb--current" : "crumb",
      text: displayName(name),
      title: displayName(name),
      onclick: isLast
        ? null
        : () => {
            if (state.suppressClick) return;
            navigate(path);
          },
    });
    bindFolderDrop(crumbBtn, path);
    nav.append(crumbBtn);
  });

  const up = $("btn-up");
  up.disabled = parts.length === 0;
  if (parts.length) {
    bindFolderDrop(up, parts.slice(0, -1).join("/"));
  } else {
    up.dataset.dropPath = "";
  }
}

function renderActionBar() {
  const count = state.selection.size;
  show($("default-bar"), count === 0);
  show($("selection-bar"), count > 0);

  if (count > 0) {
    $("sel-label").textContent = `${count} selected`;
    const chosen = selectedItems();
    // Files download directly; folders (and multi-select) stream as zip.
    $("btn-download").disabled = chosen.length === 0;
    $("btn-rename").disabled = count !== 1;
    $("btn-copy-link").lastChild.textContent = count === 1 ? "Copy link" : "Copy links";
    const copyBtn = $("btn-copy-path");
    if (copyBtn) {
      let hasText = false;
      const copyLabel = count === 1 ? "Copy path" : "Copy paths";
      for (const node of copyBtn.childNodes) {
        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
          node.textContent = copyLabel;
          hasText = true;
        }
      }
      if (!hasText) copyBtn.append(document.createTextNode(copyLabel));
    }
    const delBtn = $("btn-delete");
    if (delBtn) {
      const forever = chosen.length > 0 && chosen.every((item) => isUnderRecycle(item.path));
      // HTML is: <svg>…</svg>Delete  — replace trailing text node(s)
      const label = forever ? "Delete forever" : "Delete";
      let hasText = false;
      for (const node of delBtn.childNodes) {
        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
          node.textContent = label;
          hasText = true;
        }
      }
      if (!hasText) delBtn.append(document.createTextNode(label));
    }
  }

  const emptyBtn = $("btn-empty-recycle");
  if (emptyBtn) {
    const showEmpty = state.isAdmin && isRecycleRoot(state.path);
    emptyBtn.hidden = !showEmpty;
  }

  const visible = visibleItems();
  const label = state.loading ? "Loading…" : `${visible.length} item${visible.length === 1 ? "" : "s"}`;
  $("count-label").textContent = label;
  $("list-foot-count").textContent = label;

  $("btn-view-list").setAttribute("aria-pressed", String(state.view === "list"));
  $("btn-view-grid").setAttribute("aria-pressed", String(state.view === "grid"));

  const hiddenBtn = $("btn-hidden");
  const hiddenLabel = state.hideDotfiles ? "Show dot files" : "Hide dot files";
  hiddenBtn.setAttribute("aria-pressed", String(state.hideDotfiles));
  hiddenBtn.title = hiddenLabel;
  hiddenBtn.setAttribute("aria-label", hiddenLabel);
  hiddenBtn.querySelector("use").setAttribute("href", state.hideDotfiles ? "#i-eye-off" : "#i-eye");

  const rows = $("rows");
  rows.dataset.density = state.density;
  rows.dataset.sort = state.sortAsc ? "asc" : "desc";
  rows.dataset.sortKey = state.sortKey;
  const sortLabel = $("sort-by-label");
  if (sortLabel) sortLabel.textContent = SORT_LABELS[state.sortKey] || "Name";
  const sortBy = $("btn-sort-by");
  if (sortBy) {
    sortBy.classList.toggle("is-desc", !state.sortAsc);
    sortBy.title = `Sort by ${SORT_LABELS[state.sortKey] || "name"} (${state.sortAsc ? "ascending" : "descending"})`;
  }
  for (const btn of document.querySelectorAll(".rows__head .sort")) {
    const active = btn.dataset.sort === state.sortKey;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-pressed", String(active));
  }

  const allSelected = visible.length > 0 && visible.every((item) => state.selection.has(item.path));
  $("check-all").setAttribute("aria-checked", String(allSelected));
}

function renderContent() {
  const visible = visibleItems();
  const searching = state.query.trim().length > 0;
  // Skeleton only after the delay, or immediately when there's nothing to keep on screen.
  const showSkel = state.loading && (state.showSkeleton || state.items.length === 0);
  const settled = !state.loading;

  const panels = {
    loading: showSkel,
    error: settled && !!state.error,
    nomatch: settled && !state.error && visible.length === 0 && searching,
    empty: settled && !state.error && visible.length === 0 && !searching,
    // While a fast load is in flight, keep the previous listing visible (stale).
    list: !showSkel && !state.error && visible.length > 0 && state.view === "list",
    grid: !showSkel && !state.error && visible.length > 0 && state.view === "grid",
  };

  const content = document.querySelector(".content");
  if (content) {
    content.classList.toggle("is-refreshing", state.loading && !showSkel && state.items.length > 0);
  }

  show($("state-loading"), panels.loading);
  show($("state-error"), panels.error);
  show($("state-nomatch"), panels.nomatch);
  show($("state-empty"), panels.empty);
  show($("state-list"), panels.list);
  show($("state-grid"), panels.grid);

  if (panels.loading) {
    $("loading-text").textContent = `Reading ${folderLabel(state.path)} from the NAS…`;
    renderSkeleton();
    return;
  }

  if (panels.error) {
    const error = state.error;
    const denied = error instanceof ApiError && error.isPermission;
    const locked = error instanceof ApiError && [423, 428].includes(error.status) && state.share.startsWith("~");
    show($("btn-unlock-personal"), locked);
    $("error-title").textContent = locked ? "Folder is locked" : denied ? "You don't have access here" : "Can't open this folder";
    $("error-text").textContent = denied
      ? `Your NAS account ${state.me?.nas_username || ""} isn't allowed to read ${folderLabel(state.path)}. Ask the NAS admin if you should have access.`
      : error.message;
    return;
  }

  if (panels.nomatch) {
    $("nomatch-text").textContent = `Nothing named “${state.query.trim()}” in this folder list — check the search dropdown for nested files.`;
    return;
  }

  if (panels.list) renderRows(visible);
  if (panels.grid) renderGrid(visible);
}

function renderSkeleton() {
  const host = $("skeleton-rows");
  host.textContent = "";
  const widths = [72, 55, 84, 40, 66, 78, 48, 60, 70];
  widths.forEach((width, index) => {
    host.append(
      el("div", { class: "skel__row", style: `animation-delay:${(index * 0.09).toFixed(2)}s` }, [
        el("div", { class: "skel__chip" }),
        el("div", { class: "skel__name" }, [
          el("div", { class: "skel__chip" }),
          el("div", { class: "skel__bar", style: `width:${width}%` }),
        ]),
        el("div", { class: "skel__bar", style: "width:70%" }),
        el("div", { class: "skel__bar", style: "width:56%" }),
        el("div", { class: "skel__bar", style: "width:80%" }),
        el("div"),
      ]),
    );
  });
}

function clearPortaledMenus() {
  for (const menu of document.querySelectorAll(".menu.menu--portal")) {
    menu.remove();
  }
  const btn = $("btn-upload");
  if (btn) btn.setAttribute("aria-expanded", "false");
  const sortBy = $("btn-sort-by");
  if (sortBy) sortBy.setAttribute("aria-expanded", "false");
  for (const more of document.querySelectorAll(".crumb--more")) {
    more.setAttribute("aria-expanded", "false");
  }
}

function placeFixedMenu(menu, top, left) {
  const mh = menu.offsetHeight || 200;
  const mw = menu.offsetWidth || 180;
  let t = top;
  let l = left;
  if (t + mh > window.innerHeight - 8) {
    t = Math.max(8, t - mh - 8);
  }
  if (l + mw > window.innerWidth - 8) {
    l = Math.max(8, window.innerWidth - mw - 8);
  }
  if (l < 8) l = 8;
  if (t < 8) t = 8;
  menu.style.position = "fixed";
  menu.style.top = `${Math.round(t)}px`;
  menu.style.left = `${Math.round(l)}px`;
  menu.style.right = "auto";
  menu.style.bottom = "auto";
  menu.style.zIndex = "200";
}

function positionFixedMenu(menu, anchor) {
  const rect = anchor.getBoundingClientRect();
  placeFixedMenu(menu, rect.bottom + 4, rect.right - (menu.offsetWidth || 180));
}

function positionFixedMenuAtPoint(menu, x, y) {
  placeFixedMenu(menu, y + 2, x + 2);
}

function openRowMenu(item, index, anchorPoint = null) {
  state.openMenu = item.path;
  state.menuAnchor = anchorPoint;
  // Right-click inside an existing multi-selection keeps it (menu acts on all);
  // otherwise the clicked item becomes the selection.
  if (!state.selection.has(item.path)) {
    state.selection = new Set([item.path]);
    state.anchorIndex = index;
  }
  render();
}

/** Kick off server-side pre-generation for the current listing's thumbs so
 * scrolling (and the other view mode) hits warm cache. Fire-and-forget. */
function warmFolderThumbs() {
  const candidates = state.items.filter(thumbCandidate);
  if (!candidates.length) return;
  const size = state.view === "grid" ? 256 : 64;
  api.warmThumbnails(candidates.map((item) => item.path), size).catch(() => {
    /* prefetch is best-effort — on-demand generation still works */
  });
}

/** Server can thumbnail this item (image sans svg, or video). */
function thumbCandidate(item) {
  if (item.is_dir || item.is_link) return false;
  const kind = previewKind(item);
  if (kind === "video") return true;
  return kind === "image" && fileExt(item.name) !== "svg";
}

/**
 * Type icon immediately; swap to a server thumb only after it loads.
 * Avoids blank/broken squares while ffmpeg/Pillow generate (or on cold cache).
 */
function thumbOrIcon(item, kind, { size, imgClass, iconPx }) {
  const fallback = icon(kind.icon, iconPx, `color:${kind.color}`);
  if (!thumbCandidate(item)) return fallback;

  const wrap = el("span", { class: "thumb-slot" }, [fallback]);
  const img = el("img", {
    class: imgClass,
    loading: "lazy",
    decoding: "async",
    alt: "",
    draggable: "false",
  });
  img.onload = () => {
    if (wrap.isConnected) wrap.replaceChildren(img);
  };
  img.onerror = () => {
    /* keep the type icon */
  };
  img.src = api.thumbnailUrl(item.path, size, item.mtime);
  return wrap;
}

function renderRows(items) {
  const body = $("rows-body");
  body.textContent = "";
  clearPortaledMenus();

  items.forEach((item, index) => {
    const kind = describeKind(item);
    const selected = state.selection.has(item.path);

    const row = el("div", {
      class: `row${item.is_dir ? " row--folder" : ""}${index === state.cursorIndex ? " is-cursor" : ""}`,
      role: "row",
      "aria-selected": String(selected),
      "data-path": item.path,
      ondblclick: () => openItem(item),
      onclick: (event) => {
        if (state.suppressClick) return;
        if (event.target.closest(".row__dots, .menu, .checkbox")) return;
        // Folders open; media files preview; other files select (checkbox also selects).
        if (item.is_dir || previewKind(item)) {
          openItem(item);
          return;
        }
        selectRow(item, index, event);
      },
      oncontextmenu: (event) => {
        event.preventDefault();
        event.stopPropagation();
        openRowMenu(item, index, { x: event.clientX, y: event.clientY });
      },
    });
    bindItemDrag(row, item);
    if (item.is_dir) bindFolderDrop(row, item.path);

    row.append(
      el("button", {
        type: "button",
        class: "checkbox",
        role: "checkbox",
        "aria-checked": String(selected),
        "aria-label": `Select ${item.name}`,
        onclick: (event) => {
          event.stopPropagation();
          toggleSelection(item.path);
          state.anchorIndex = index;
          render();
        },
      }, [icon("#i-check", 11)]),
    );

    const label = displayName(item.name);
    const name = el("div", { class: "row__name" }, [
      thumbOrIcon(item, kind, { size: 64, imgClass: "row__thumb", iconPx: 28 }),
      el("span", { class: "row__name-text", text: label, title: label }),
    ]);
    if (item.is_link) {
      name.append(icon("#i-ext", 12, "flex:0 0 auto;opacity:.6"));
    }
    row.append(name);

    row.append(el("span", { class: "row__kind", text: kind.label }));
    row.append(
      el("span", {
        class: "row__size",
        text: itemSizeLabel(item),
        title: item.is_dir && item.size_incomplete ? "Approximate — folder is large or still scanning" : null,
      }),
    );
    row.append(el("span", { class: "row__mod", text: formatModified(item.mtime), title: formatExact(item.mtime) }));

    const dotsBtn = el(
      "button",
      {
        type: "button",
        class: "row__dots",
        "aria-label": `Actions for ${item.name}`,
        "aria-expanded": String(state.openMenu === item.path),
        onclick: (event) => {
          event.stopPropagation();
          // Toggle when re-clicking ⋯ on the same row without a cursor anchor.
          if (state.openMenu === item.path && !state.menuAnchor) {
            state.openMenu = null;
            state.menuAnchor = null;
            render();
            return;
          }
          openRowMenu(item, index, null);
        },
      },
      [icon("#i-dots", 15)],
    );
    row.append(dotsBtn);

    body.append(row);

    // Portal to body — .panel { overflow:hidden } was clipping the menu.
    if (state.openMenu === item.path) {
      const menu = buildRowMenu(item);
      document.body.append(menu);
      if (state.menuAnchor) {
        positionFixedMenuAtPoint(menu, state.menuAnchor.x, state.menuAnchor.y);
      } else {
        positionFixedMenu(menu, dotsBtn);
      }
    }
  });
}

function buildRowMenu(item) {
  const close = () => {
    state.openMenu = null;
    state.menuAnchor = null;
    clearPortaledMenus();
    render();
  };

  const selected = selectedItems();
  const multi = selected.length > 1 && state.selection.has(item.path);

  const entries = [];
  if (multi) {
    entries.push({
      label: `Download ${selected.length} as ZIP`,
      symbol: "#i-download",
      run: () => downloadItems(selected),
    });
    entries.push({
      label: `Move ${selected.length} to…`,
      symbol: "#i-move",
      run: () => openMoveModal(selected),
    });
  } else {
    if (item.is_dir) {
      entries.push({ label: "Open", symbol: "#i-chev-r", run: () => openItem(item) });
      entries.push({
        label: "Download as ZIP",
        symbol: "#i-download",
        run: () => downloadItems([item]),
      });
      if (!isRecycleRoot(item.path) && !isUnderRecycle(item.path)) {
        entries.push({ label: "Share…", symbol: "#i-share", run: () => openShareModal(item) });
      }
    } else {
      if (previewKind(item)) {
        entries.push({ label: "Preview", symbol: "#i-ext", run: () => openPreview(item) });
      }
      entries.push({ label: "Download", symbol: "#i-download", run: () => downloadItems([item]) });
      entries.push({ label: "Share…", symbol: "#i-share", run: () => openShareModal(item) });
    }
    entries.push(
      { label: "Rename", symbol: "#i-pencil", run: () => openRenameModal(item) },
      { label: "Move to…", symbol: "#i-move", run: () => openMoveModal([item]) },
    );
    if (item.is_dir && !isRecycleRoot(item.path) && !isUnderRecycle(item.path)) {
      entries.push({
        label: isFavorite(item.path) ? "Remove favorite" : "Add to favorites",
        symbol: "#i-star",
        run: () => {
          toggleFavorite(item.path);
          renderQuickAccess();
        },
      });
    }
  }

  entries.push({
    label: multi ? `Copy ${selected.length} links` : "Copy link",
    symbol: "#i-copy",
    run: () => copyItemLinks(multi ? selected : [item]),
  });
  entries.push({
    label: multi ? `Copy ${selected.length} paths` : "Copy path",
    symbol: "#i-copy",
    run: () => copyItemPaths(multi ? selected : [item]),
  });

  const menu = el("div", {
    class: "menu menu--portal",
    role: "menu",
    onclick: (event) => event.stopPropagation(),
  });
  for (const entry of entries) {
    menu.append(
      el(
        "button",
        {
          type: "button",
          role: "menuitem",
          onclick: () => {
            close();
            entry.run();
          },
        },
        [icon(entry.symbol, 14), entry.label],
      ),
    );
  }
  menu.append(el("div", { class: "menu__sep" }));
  const inTrash = isUnderRecycle(item.path);
  const deleteLabel = multi
    ? inTrash
      ? `Delete ${selected.length} forever`
      : `Move ${selected.length} to Recycle`
    : inTrash
      ? "Delete forever"
      : "Move to Recycle";
  menu.append(
    el(
      "button",
      {
        type: "button",
        role: "menuitem",
        class: "danger",
        onclick: () => {
          close();
          openDeleteModal(multi ? selected : [item]);
        },
      },
      [icon(inTrash ? "#i-trash" : "#i-recycle", 14), deleteLabel],
    ),
  );
  return menu;
}

function renderGrid(items) {
  const grid = $("state-grid");
  grid.textContent = "";
  clearPortaledMenus();

  items.forEach((item, index) => {
    const kind = describeKind(item);
    const selected = state.selection.has(item.path);
    const tile = el(
      "div",
      {
        class: `tile${item.is_dir ? " tile--folder" : ""}${index === state.cursorIndex ? " is-cursor" : ""}`,
        role: "button",
        tabindex: "0",
        "aria-selected": String(selected),
        "data-path": item.path,
        ondblclick: () => openItem(item),
        onclick: (event) => {
          if (state.suppressClick) return;
          if (item.is_dir || previewKind(item)) {
            openItem(item);
            return;
          }
          selectRow(item, index, event);
        },
        onkeydown: (event) => {
          if (event.key === "Enter") openItem(item);
        },
        oncontextmenu: (event) => {
          event.preventDefault();
          event.stopPropagation();
          openRowMenu(item, index, { x: event.clientX, y: event.clientY });
        },
      },
      [
        (() => {
          const box = el("div", { class: "tile__thumb" });
          box.append(thumbOrIcon(item, kind, { size: 256, imgClass: "tile__img", iconPx: 30 }));
          return box;
        })(),
        el("div", { class: "tile__name", text: displayName(item.name), title: displayName(item.name) }),
        el("div", { class: "tile__meta", text: item.is_dir ? itemSizeLabel(item) : formatSize(item.size) }),
      ],
    );
    bindItemDrag(tile, item);
    if (item.is_dir) bindFolderDrop(tile, item.path);
    grid.append(tile);

    // Portal like renderRows — the grid clips absolutely-positioned children.
    if (state.openMenu === item.path) {
      const menu = buildRowMenu(item);
      document.body.append(menu);
      if (state.menuAnchor) {
        positionFixedMenuAtPoint(menu, state.menuAnchor.x, state.menuAnchor.y);
      } else {
        positionFixedMenu(menu, tile);
      }
    }
  });
}

/* ---------------------------------------------------------------------------
   Selection
   --------------------------------------------------------------------------- */
function toggleSelection(path) {
  if (state.selection.has(path)) state.selection.delete(path);
  else state.selection.add(path);
}

function selectRow(item, index, event) {
  const visible = visibleItems();

  if (event.shiftKey && state.anchorIndex != null) {
    const from = Math.min(state.anchorIndex, index);
    const to = Math.max(state.anchorIndex, index);
    state.selection = new Set(visible.slice(from, to + 1).map((entry) => entry.path));
  } else if (event.metaKey || event.ctrlKey) {
    toggleSelection(item.path);
    state.anchorIndex = index;
  } else {
    const onlyThis = state.selection.size === 1 && state.selection.has(item.path);
    state.selection = onlyThis ? new Set() : new Set([item.path]);
    state.anchorIndex = index;
  }
  state.cursorIndex = index;
  state.openMenu = null;
  render();
}

/** Columns in the current grid layout (1 in list view). */
function gridColumns() {
  const grid = $("state-grid");
  if (grid.hidden) return 1;
  return getComputedStyle(grid).gridTemplateColumns.split(" ").length || 1;
}

/** Keyboard cursor: move by delta through visibleItems(), optionally extending. */
function scrollItemIntoView(item) {
  if (!item) return;
  const container = state.view === "grid" ? $("state-grid") : $("rows-body");
  const node = container?.querySelector(`[data-path="${CSS.escape(item.path)}"]`);
  node?.scrollIntoView({ block: "nearest" });
}

function moveCursor(delta, extend) {
  const visible = visibleItems();
  if (!visible.length) return;
  let next;
  if (state.cursorIndex === null || state.cursorIndex >= visible.length) {
    next = delta > 0 ? 0 : visible.length - 1;
  } else {
    next = Math.min(visible.length - 1, Math.max(0, state.cursorIndex + delta));
  }
  state.cursorIndex = next;
  const item = visible[next];
  if (extend) {
    if (state.anchorIndex === null) state.anchorIndex = next;
    const from = Math.min(state.anchorIndex, next);
    const to = Math.max(state.anchorIndex, next);
    state.selection = new Set(visible.slice(from, to + 1).map((entry) => entry.path));
  } else {
    state.selection = new Set([item.path]);
    state.anchorIndex = next;
  }
  state.openMenu = null;
  render();
  scrollItemIntoView(item);
}

const JUMP_RESET_MS = 700;
let jumpBuffer = "";
let jumpTimer = 0;

function jumpToTyped(ch) {
  jumpBuffer += ch.toLowerCase();
  if (jumpTimer) clearTimeout(jumpTimer);
  jumpTimer = setTimeout(() => {
    jumpBuffer = "";
    jumpTimer = 0;
  }, JUMP_RESET_MS);

  const visible = visibleItems();
  if (!visible.length) return;
  const start = state.cursorIndex == null ? 0 : state.cursorIndex + 1;
  let idx = -1;
  for (let i = 0; i < visible.length; i += 1) {
    const j = (start + i) % visible.length;
    if (displayName(visible[j].name).toLowerCase().startsWith(jumpBuffer)) {
      idx = j;
      break;
    }
  }
  if (idx < 0) return;
  const item = visible[idx];
  state.cursorIndex = idx;
  state.anchorIndex = idx;
  state.selection = new Set([item.path]);
  state.openMenu = null;
  render();
  scrollItemIntoView(item);
}

function openItem(item) {
  if (item.is_dir) {
    navigate(item.path);
    return;
  }
  if (previewKind(item)) {
    openPreview(item);
    return;
  }
  downloadItems([item]);
}

function downloadItems(items) {
  if (!items.length) return;
  const folders = items.filter((item) => item.is_dir);
  const files = items.filter((item) => !item.is_dir);

  // Large Chromium downloads: parallel Range GETs into the save picker (progress tray).
  // Small files / Safari: native <a download> so they land in Downloads.
  if (items.length === 1 && files.length === 1) {
    const file = files[0];
    const size = Math.max(0, Number(file.size) || 0);
    if (size >= api.DOWNLOAD_RANGE_THRESHOLD && typeof window.showSaveFilePicker === "function") {
      queueDownload({
        name: file.name,
        filename: file.name,
        url: api.downloadUrl(file.path),
        size,
        kind: describeKind(file),
        saveHandlePromise: api.saveFilePicker(file.name),
        parallel: true,
      });
      return;
    }
    api.startBrowserDownload(api.downloadUrl(file.path), file.name);
    return;
  }

  const paths = items.map((item) => item.path);
  let zipName;
  let size = 0;
  if (items.length === 1 && folders.length === 1) {
    zipName = `${folders[0].name}.zip`;
  } else {
    zipName = `Drive-${items.length}-items.zip`;
    size = files.reduce((sum, item) => sum + (Number(item.size) || 0), 0);
  }
  queueDownload({
    name: zipName,
    filename: zipName,
    url: api.downloadZipUrl(paths),
    size,
    kind: describeKind({ name: zipName, is_dir: false }),
    detailLabel: items.length === 1 ? "Folder zip" : `${items.length} items`,
    // Start the save picker now so Chromium still counts this as a user gesture.
    saveHandlePromise: api.saveFilePicker(zipName),
  });
}

/* ---------------------------------------------------------------------------
   Media / document preview lightbox (implementation lives in viewer.js)
   --------------------------------------------------------------------------- */
function openPreview(item) {
  if (!previewKind(item)) {
    downloadItems([item]);
    return;
  }
  import("./viewer.js?v=20261027-mac").then(({ openPreview: openViewer }) => {
    openViewer(item, {
      siblings: visibleItems().filter((entry) => previewKind(entry)),
      downloadUrl: api.downloadUrl,
      download: downloadItems,
      openModal,
      closeModal,
      onScrimReady: () => $("scrim").classList.add("scrim--viewer"),
    });
  });
}

/* ---------------------------------------------------------------------------
   Drag-and-drop moves (in-app). External OS file drops still upload.
   --------------------------------------------------------------------------- */
function parentPath(path) {
  const parts = pathParts(path);
  return parts.slice(0, -1).join("/");
}

function baseName(path) {
  const parts = pathParts(path);
  return parts.length ? parts[parts.length - 1] : "";
}

/**
 * Undo a delete/move: move each item back to where it came from.
 * `moves` = [{from, toDir, name?}] — `name` set when the recycle bin may have
 * versioned the filename (name.#1) and it should be renamed back.
 */
async function undoMoves(moves) {
  let restored = 0;
  let firstError = null;
  for (const move of moves) {
    try {
      await api.moveItem(move.from, move.toDir);
      const landedName = baseName(move.from);
      if (move.name && landedName !== move.name) {
        const landed = move.toDir ? `${move.toDir}/${landedName}` : landedName;
        await api.renameItem(landed, move.name);
      }
      restored += 1;
    } catch (error) {
      firstError = firstError || error;
    }
  }
  invalidateTreeAround(moves.flatMap((m) => [m.from, m.toDir]), "");
  if (restored) toast(`Restored ${restored} item${restored === 1 ? "" : "s"}`);
  if (firstError) handleMutationError(firstError);
  await loadFolder(state.path);
  renderTree();
}

function isInternalDrag(event) {
  const types = [...(event.dataTransfer?.types || [])];
  if (types.includes("Files")) return false;
  return Boolean(state.dndPaths) || types.includes(IFD_DND);
}

function parseDragPaths(event) {
  if (state.dndPaths?.length) return state.dndPaths.slice();
  try {
    const raw = event.dataTransfer?.getData(IFD_DND) || event.dataTransfer?.getData("text/plain") || "";
    if (!raw) return [];
    if (raw.startsWith("[")) {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.filter((p) => typeof p === "string") : [];
    }
    return raw.split("\n").map((s) => s.trim()).filter(Boolean);
  } catch {
    return [];
  }
}

/** True if `path` is the same as `root` or nested inside it. */
function isSelfOrDescendant(path, root) {
  if (path === root) return true;
  if (!root) return false;
  return path.startsWith(`${root}/`);
}

/**
 * Can we move every path into destDir?
 * Rejects dropping a folder into itself / a child, and pure no-ops.
 */
function canMoveTo(paths, destDir) {
  if (!paths?.length) return false;
  let actionable = 0;
  for (const path of paths) {
    if (!path) continue;
    // Don't drop a folder onto itself or into one of its children.
    if (isSelfOrDescendant(destDir, path)) return false;
    if (parentPath(path) === destDir) continue;
    actionable += 1;
  }
  return actionable > 0;
}

function clearDropHighlights() {
  document.querySelectorAll(".drop-target").forEach((node) => node.classList.remove("drop-target"));
}

function pathsForDrag(item) {
  if (state.selection.has(item.path) && state.selection.size > 1) {
    return [...state.selection];
  }
  return [item.path];
}

function bindItemDrag(node, item) {
  node.draggable = true;
  node.addEventListener("dragstart", (event) => {
    // Don't start a move from checkbox / menu / tree chevron controls.
    if (event.target.closest?.(".checkbox, .row__dots, .menu, .tree-chev")) {
      event.preventDefault();
      return;
    }
    const paths = pathsForDrag(item);
    state.dndPaths = paths;
    event.dataTransfer.setData(IFD_DND, JSON.stringify(paths));
    event.dataTransfer.setData("text/plain", paths.join("\n"));
    event.dataTransfer.effectAllowed = "move";
    try {
      event.dataTransfer.setDragImage(node, 16, 16);
    } catch {
      /* some browsers reject setDragImage on certain nodes */
    }
    node.classList.add("is-dragging");
    document.body.classList.add("is-dnd-move");
    // Dim other selected sources when multi-dragging.
    if (paths.length > 1) {
      document.querySelectorAll(".row[aria-selected='true'], .tile[aria-selected='true']").forEach((el) => {
        el.classList.add("is-dragging");
      });
    }
  });
  node.addEventListener("dragend", () => {
    state.dndPaths = null;
    document.body.classList.remove("is-dnd-move");
    document.querySelectorAll(".is-dragging").forEach((el) => el.classList.remove("is-dragging"));
    clearDropHighlights();
    state.suppressClick = true;
    setTimeout(() => {
      state.suppressClick = false;
    }, 80);
  });
}

function bindFolderDrop(node, destPath) {
  // Dest can change across re-renders (e.g. btn-up parent); store on the node.
  node.dataset.dropPath = destPath == null ? "" : String(destPath);
  if (node.dataset.dropBound === "1") return;
  node.dataset.dropBound = "1";

  node.addEventListener("dragover", (event) => {
    if (!isInternalDrag(event)) return;
    const dest = node.dataset.dropPath ?? "";
    const paths = state.dndPaths || parseDragPaths(event);
    if (!canMoveTo(paths, dest)) {
      if (event.dataTransfer) event.dataTransfer.dropEffect = "none";
      node.classList.remove("drop-target");
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    node.classList.add("drop-target");
  });
  node.addEventListener("dragleave", (event) => {
    const next = event.relatedTarget;
    if (next instanceof Node && node.contains(next)) return;
    node.classList.remove("drop-target");
  });
  node.addEventListener("drop", (event) => {
    if (!isInternalDrag(event)) return;
    event.preventDefault();
    event.stopPropagation();
    node.classList.remove("drop-target");
    const dest = node.dataset.dropPath ?? "";
    const paths = parseDragPaths(event);
    if (!canMoveTo(paths, dest)) return;
    void movePathsTo(paths, dest);
  });
}

function invalidateTreeAround(paths, dest) {
  const keys = new Set([dest, ""]);
  for (const path of paths) {
    keys.add(parentPath(path));
    keys.add(path);
  }
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(state.treeChildren, key)) {
      delete state.treeChildren[key];
    }
  }
}

async function movePathsTo(paths, dest) {
  const unique = [...new Set(paths)].filter(Boolean);
  const toMove = unique.filter(
    (path) => parentPath(path) !== dest && !isSelfOrDescendant(dest, path),
  );
  if (!toMove.length) return;

  let ok = 0;
  let firstError = null;
  const restores = [];
  for (const path of toMove) {
    try {
      await api.moveItem(path, dest);
      ok += 1;
      state.selection.delete(path);
      const landed = dest ? `${dest}/${baseName(path)}` : baseName(path);
      restores.push({ from: landed, toDir: parentPath(path) });
    } catch (error) {
      firstError = firstError || error;
    }
  }

  invalidateTreeAround(toMove, dest);
  if (ok) {
    toast(`Moved ${ok} item${ok === 1 ? "" : "s"} to ${folderLabel(dest)}`, "ok", {
      actionLabel: "Undo",
      onAction: () => void undoMoves(restores),
    });
  }
  if (firstError) handleMutationError(firstError);
  await loadFolder(state.path);
  // Refresh root shortcuts if anything moved to/from root.
  if (dest === "" || toMove.some((p) => parentPath(p) === "")) {
    await refreshShortcuts();
  } else {
    renderTree();
  }
}

/* ---------------------------------------------------------------------------
   Modals
   --------------------------------------------------------------------------- */
let modalKeyHandler = null;
/** Teardown for the open modal's own resources (player timers, listeners). */
let modalCloseHandler = null;

function closeModal() {
  const scrim = $("scrim");
  if (modalCloseHandler) {
    const handler = modalCloseHandler;
    modalCloseHandler = null;
    handler();
  }
  scrim.querySelectorAll("video, audio").forEach((media) => {
    try {
      media.pause();
    } catch {
      /* ignore */
    }
  });
  scrim.textContent = "";
  scrim.classList.remove("scrim--viewer");
  show(scrim, false);
  if (modalKeyHandler) {
    window.removeEventListener("keydown", modalKeyHandler, true);
    modalKeyHandler = null;
  }
}

function openModal(node, { onSubmit, onKey, onClose } = {}) {
  const scrim = $("scrim");
  // Tear down whatever was open before swapping content in.
  if (modalCloseHandler) {
    const previous = modalCloseHandler;
    modalCloseHandler = null;
    previous();
  }
  scrim.textContent = "";
  scrim.classList.remove("scrim--viewer");
  modalCloseHandler = onClose || null;
  scrim.onclick = (event) => {
    if (event.target === scrim) closeModal();
  };
  scrim.append(node);
  show(scrim, true);

  modalKeyHandler = (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      closeModal();
      return;
    }
    if (onKey) onKey(event);
    if (event.key === "Enter" && onSubmit && event.target.tagName === "INPUT") {
      event.preventDefault();
      onSubmit();
    }
  };
  window.addEventListener("keydown", modalKeyHandler, true);

  const field = node.querySelector("input");
  if (field) {
    field.focus();
    field.select();
  }
}

function modalFooter(children) {
  return el("div", { class: "modal__foot" }, children);
}

function openPersonalUnlock(shareId, needsOwnerSignIn = false) {
  const owner = shareId.slice(1);
  if (location.protocol !== "https:" && !["localhost", "127.0.0.1"].includes(location.hostname)) {
    const base = state.me?.public_base_url || location.origin;
    openModal(el("div", { class: "modal" }, [el("div", { class: "modal__body" }, [
      el("h3", { text: "Use a secure connection to unlock" }),
      el("p", { class: "modal__hint", text: "Enter your NAS password and encryption key over HTTPS." }),
      el("a", { class: "btn btn--primary", text: "Open secure Drive", href: `${base}/?nolan=1#/?share=${encodeURIComponent(shareId)}` }),
    ])]));
    return;
  }
  const input = el("input", { class: "field", type: "password", autocomplete: "off",
    placeholder: "Encryption password", "aria-label": "Encryption password" });
  const file = el("input", { type: "file", "aria-label": "Encryption key file" });
  const nasPassword = el("input", { class: "field", type: "password", autocomplete: "current-password",
    placeholder: "NAS account password", "aria-label": "NAS account password" });
  const otp = el("input", { class: "field", autocomplete: "one-time-code", inputmode: "numeric",
    placeholder: "Authenticator code", "aria-label": "Authenticator code", hidden: true });
  const authFields = el("div", { hidden: !needsOwnerSignIn }, [
    el("p", { class: "modal__hint", text: `Your folder stays private. Sign in to the NAS as ${owner} to authorize unlocking and daily relocking. Your NAS password is not saved.` }),
    nasPassword, otp,
  ]);
  const keyFields = el("div", { hidden: needsOwnerSignIn }, [input,
    el("p", { class: "modal__hint", text: "Or choose your encryption key file:" }), file]);
  const error = el("p", { class: "modal__hint", role: "alert" });
  let needsOtp = false;
  let busy = false;
  let cancelled = false;
  const submit = async () => {
    if (busy) return;
    const selected = file.files?.[0];
    if (selected && selected.size > 65536) { error.textContent = "Key files must be under 64 KB."; return; }
    busy = true;
    button.disabled = true;
    error.textContent = "";
    try {
      if (needsOwnerSignIn) {
        if (needsOtp ? !otp.value.trim() : !nasPassword.value) {
          throw new Error(needsOtp ? "Enter your authenticator code." : "Enter your NAS account password.");
        }
        const result = await api.authenticatePersonal(owner, needsOtp ? "" : nasPassword.value, needsOtp ? otp.value : "");
        nasPassword.value = "";
        if (cancelled) return;
        if (result.need_otp) {
          needsOtp = true;
          nasPassword.hidden = true;
          otp.hidden = false;
          otp.focus();
          return;
        }
        otp.value = "";
        needsOwnerSignIn = false;
        authFields.hidden = true;
        keyFields.hidden = false;
        button.textContent = "Unlock";
        input.focus();
        return;
      }
      const key = selected ? await selected.text() : input.value;
      if (!key) throw new Error("Enter the encryption password or choose its key file.");
      const result = await api.unlockPersonal(owner, key, Boolean(selected));
      if (result.locked) throw new Error("UGOS is still unlocking this folder. Try opening it again shortly.");
      if (cancelled) return;
      input.value = "";
      file.value = "";
      const entry = state.shares?.find((s) => s.id === shareId);
      if (entry) entry.locked = false;
      closeModal();
      if (shareId === state.share) { await loadFolder(state.path); await loadUsage(); }
      else await switchShare(shareId);
    } catch (e) {
      if (!cancelled && e instanceof ApiError && e.status === 428) {
        needsOwnerSignIn = true;
        authFields.hidden = false;
        keyFields.hidden = true;
        button.textContent = "Sign in";
        nasPassword.focus();
      }
      if (!cancelled) error.textContent = e.message || "Could not unlock this folder.";
    } finally {
      busy = false;
      button.disabled = false;
    }
  };
  const button = el("button", { type: "button", class: "btn btn--primary btn--modal", text: needsOwnerSignIn ? "Sign in" : "Unlock", onclick: submit });
  const body = el("div", { class: "modal__body" }, [
    el("h3", { text: `Unlock ${owner}` }),
    el("p", { class: "modal__hint", text: "Use your UGOS encryption password or key file. The folder relocks everywhere, including Finder, after 24 hours." }),
    authFields, keyFields, error,
    modalFooter([el("button", { type: "button", class: "btn btn--modal", text: "Cancel", onclick: closeModal }), button]),
  ]);
  openModal(el("div", { class: "modal" }, [body]), { onSubmit: submit, onClose: () => {
    cancelled = true;
    input.value = "";
    file.value = "";
    nasPassword.value = "";
    otp.value = "";
  } });
  if (needsOwnerSignIn) nasPassword.focus();
  else input.focus();
}

/** Shown when the NAS rejects a write — read access can still be fine. */
function openDeniedModal(message) {
  const body = el("div", { class: "modal__body" }, [
    el("div", { class: "modal__lead" }, [
      el("div", { class: "modal__badge modal__badge--warn" }, [icon("#i-lock", 17)]),
      el("div", {}, [
        el("h3", { text: "You can read this folder, but not change it" }),
        el("p", {}, [
          "Your NAS account ",
          el("code", { class: "mono", text: state.me?.nas_username || "" }),
          ` doesn't have write access to ${folderLabel(state.path)}. Downloads still work — renaming, moving and deleting don't.`,
          message ? el("br") : null,
          message ? el("span", { class: "mono", style: "font-size:11px", text: message }) : null,
        ]),
      ]),
    ]),
    el("div", { class: "modal__foot" }, [
      el("button", { type: "button", class: "btn btn--modal", text: "Got it", onclick: closeModal }),
      el("a", {
        class: "btn btn--primary btn--modal",
        href: `${ADVISER_MAILTO}&body=${encodeURIComponent(`Please grant write access to ${state.path || "the drive root"} for NAS user ${state.me?.nas_username || ""}.`)}`,
        text: "Request access",
      }),
    ]),
  ]);
  openModal(el("div", { class: "modal" }, [body]));
}

/** Route a failed mutation to the right surface. */
function handleMutationError(error) {
  if (error instanceof ApiError && error.isAuth) {
    showLogin();
    return;
  }
  if (error instanceof ApiError && error.isPermission) {
    openDeniedModal(error.message);
    return;
  }
  toast(error.message || "Something went wrong", "warn");
}

function openNewFolderModal() {
  const input = el("input", { class: "field", placeholder: "Folder name", "aria-label": "Folder name" });

  const submit = async () => {
    const name = input.value.trim();
    if (!name) return;
    closeModal();
    try {
      await api.makeDir(state.path, name);
      toast(`Created ${name}`);
      await loadFolder(state.path);
    } catch (error) {
      handleMutationError(error);
    }
  };

  const body = el("div", { class: "modal__body" }, [
    el("h3", { text: "New folder" }),
    el("p", { class: "modal__hint", text: `Created in ${folderLabel(state.path)}.` }),
    input,
    modalFooter([
      el("button", { type: "button", class: "btn btn--modal", text: "Cancel", onclick: closeModal }),
      el("button", { type: "button", class: "btn btn--primary btn--modal", text: "Create folder", onclick: submit }),
    ]),
  ]);
  openModal(el("div", { class: "modal" }, [body]), { onSubmit: submit });
}

function openShareModal(item) {
  if (!item) return;

  let days = 7;
  const PRESETS = [1, 7, 14, 30];
  const kind = describeKind(item);

  const urlField = el("input", {
    class: "field",
    type: "text",
    readonly: true,
    "aria-label": "Share link",
    hidden: true,
  });
  const daysInput = el("input", {
    class: "field field--days",
    type: "number",
    min: "1",
    max: "30",
    value: "7",
    "aria-label": "Expires in days",
  });
  const copyBtn = el("button", {
    type: "button",
    class: "btn btn--primary btn--modal",
    text: "Copy link",
  });
  const chips = el("div", { class: "share-days", role: "group", "aria-label": "Link expiry" });

  const setDays = (n) => {
    const clamped = Math.min(30, Math.max(1, Number.parseInt(String(n), 10) || 7));
    days = clamped;
    daysInput.value = String(clamped);
    for (const chip of chips.querySelectorAll("button")) {
      chip.classList.toggle("is-active", Number(chip.dataset.days) === clamped);
    }
  };

  for (const n of PRESETS) {
    chips.append(
      el(
        "button",
        {
          type: "button",
          class: `search-panel__chip${n === 7 ? " is-active" : ""}`,
          "data-days": String(n),
          onclick: () => setDays(n),
        },
        [n === 1 ? "1 day" : `${n} days`],
      ),
    );
  }
  daysInput.addEventListener("change", () => setDays(daysInput.value));

  const copyLink = async () => {
    setDays(daysInput.value);
    copyBtn.disabled = true;
    try {
      const res = await api.createFileLink(item.path, days);
      const url = res?.url || "";
      urlField.value = url;
      urlField.hidden = false;
      urlField.focus();
      urlField.select();
      let copied = false;
      if (url) {
        try {
          await navigator.clipboard.writeText(url);
          copied = true;
        } catch {
          copied = false;
        }
      }
      copyBtn.replaceChildren(icon("#i-check", 15), document.createTextNode(copied ? "Copied" : "Copy link"));
      toast(copied ? "Link copied" : "Copy the link from the field", copied ? "ok" : "warn");
    } catch (error) {
      handleMutationError(error);
    } finally {
      copyBtn.disabled = false;
    }
  };
  copyBtn.addEventListener("click", () => void copyLink());

  const body = el("div", { class: "modal__body" }, [
    el("div", { class: "modal__lead" }, [
      el("div", { class: "modal__badge", style: `color:${kind.color}` }, [icon("#i-share", 17)]),
      el("div", {}, [
        el("h3", { text: item.is_dir ? "Share folder" : "Share file" }),
        el("p", { class: "modal__hint", text: displayName(item.name) }),
      ]),
    ]),
    el("p", {
      class: "modal__hint",
      text: item.is_dir
        ? "Anyone with the link can browse and download everything in this folder, including files added later — no Drive account needed. Copy it now; we don’t keep a list."
        : "Anyone with the link can view and download — no Drive account needed. Copy it now; we don’t keep a list.",
    }),
    el("label", { class: "share-days__label", text: "Expires in" }),
    el("div", { class: "share-days__row" }, [chips, daysInput]),
    urlField,
    modalFooter([
      el("button", { type: "button", class: "btn btn--modal", text: "Done", onclick: closeModal }),
      copyBtn,
    ]),
  ]);
  openModal(el("div", { class: "modal" }, [body]));
}

function openRenameModal(item) {
  const input = el("input", { class: "field", value: item.name, "aria-label": "New name" });

  const submit = async () => {
    const name = input.value.trim();
    if (!name || name === item.name) {
      closeModal();
      return;
    }
    closeModal();
    try {
      await api.renameItem(item.path, name);
      toast(`Renamed to ${name}`);
      await loadFolder(state.path);
    } catch (error) {
      handleMutationError(error);
    }
  };

  const body = el("div", { class: "modal__body" }, [
    el("h3", { text: "Rename" }),
    el("p", { class: "modal__hint", text: `In ${folderLabel(state.path)}. Keep the extension so the file still opens.` }),
    input,
    modalFooter([
      el("button", { type: "button", class: "btn btn--modal", text: "Cancel", onclick: closeModal }),
      el("button", { type: "button", class: "btn btn--primary btn--modal", text: "Rename", onclick: submit }),
    ]),
  ]);
  openModal(el("div", { class: "modal" }, [body]), { onSubmit: submit });
}

function openDeleteModal(items) {
  if (!items.length) return;
  // Never try to delete the #recycle folder itself.
  items = items.filter((item) => item.path !== "#recycle" && item.name !== "#recycle");
  if (!items.length) {
    toast("Use Empty Recycle to wipe the bin", "warn");
    return;
  }

  const list = el("div", { class: "modal__list" });
  for (const item of items) {
    const kind = describeKind(item);
    list.append(
      el("div", { class: "modal__list-item" }, [
        icon(kind.icon, 14, `color:${kind.color};flex:0 0 auto`),
        el("span", { text: displayName(item.name) }),
        el("span", { class: "mono", text: item.is_dir ? "Folder" : formatSize(item.size) }),
      ]),
    );
  }

  const allInRecycle = items.every((item) => isUnderRecycle(item.path));
  const anyInRecycle = items.some((item) => isUnderRecycle(item.path));
  // Soft-delete when share has #recycle and items aren't already in it.
  const softDelete = Boolean(state.hasRecycle) && !allInRecycle && !anyInRecycle;

  const submit = async () => {
    closeModal();
    let recycled = 0;
    let deleted = 0;
    let firstError = null;
    const restores = [];
    for (const item of items) {
      try {
        const res = await api.deleteItem(item.path);
        if (res?.action === "recycled") {
          recycled += 1;
          if (res.path) {
            restores.push({ from: res.path, toDir: parentPath(item.path), name: item.name });
          }
        } else {
          deleted += 1;
        }
      } catch (error) {
        firstError = firstError || error;
      }
    }
    if (recycled) {
      toast(
        recycled === 1
          ? "Moved to Recycle"
          : `${recycled} items moved to Recycle`,
        "ok",
        restores.length
          ? { actionLabel: "Undo", onAction: () => void undoMoves(restores) }
          : {},
      );
    }
    if (deleted) {
      toast(
        deleted === 1
          ? "Permanently deleted"
          : `${deleted} items permanently deleted`,
        recycled ? "warn" : undefined,
      );
    }
    if (firstError) handleMutationError(firstError);
    await loadFolder(state.path);
  };

  const title = softDelete
    ? items.length === 1
      ? `Move “${displayName(items[0].name)}” to Recycle?`
      : `Move ${items.length} items to Recycle?`
    : items.length === 1
      ? `Permanently delete “${displayName(items[0].name)}”?`
      : `Permanently delete ${items.length} items?`;

  const detail = softDelete
    ? "They go into this share’s Recycle folder (same as deleting in Finder/UGOS). You can restore them from Recycle, or an admin can empty the bin."
    : "This erases them for good — same as emptying Recycle. This can’t be undone.";

  const confirmLabel = softDelete
    ? items.length === 1
      ? "Move to Recycle"
      : `Move ${items.length} to Recycle`
    : items.length === 1
      ? "Delete forever"
      : `Delete ${items.length} forever`;

  const body = el("div", { class: "modal__body" }, [
    el("div", { class: "modal__lead" }, [
      el("div", { class: "modal__badge modal__badge--danger" }, [icon("#i-alert", 17)]),
      el("div", {}, [
        el("h3", { text: title }),
        el("p", { text: detail }),
      ]),
    ]),
    list,
    modalFooter([
      el("button", { type: "button", class: "btn btn--modal", text: "Cancel", onclick: closeModal }),
      el(
        "button",
        {
          type: "button",
          class: "btn btn--danger-solid",
          onclick: submit,
        },
        [icon(softDelete ? "#i-recycle" : "#i-trash", 15), confirmLabel],
      ),
    ]),
  ]);
  openModal(el("div", { class: "modal" }, [body]));
}

function openEmptyRecycleModal() {
  if (!state.isAdmin) {
    toast("Only NAS admins can empty the Recycle bin", "warn");
    return;
  }
  const submit = async () => {
    closeModal();
    try {
      const res = await api.emptyRecycle();
      const n = res?.removed ?? 0;
      toast(n ? `Emptied Recycle (${n} item${n === 1 ? "" : "s"})` : "Recycle is already empty");
      await loadFolder(state.path);
    } catch (error) {
      handleMutationError(error);
    }
  };
  const body = el("div", { class: "modal__body" }, [
    el("div", { class: "modal__lead" }, [
      el("div", { class: "modal__badge modal__badge--danger" }, [icon("#i-alert", 17)]),
      el("div", {}, [
        el("h3", { text: "Empty Recycle bin?" }),
        el("p", {
          text: "Permanently deletes everything in Recycle on this share for all users. This can’t be undone.",
        }),
      ]),
    ]),
    modalFooter([
      el("button", { type: "button", class: "btn btn--modal", text: "Cancel", onclick: closeModal }),
      el(
        "button",
        { type: "button", class: "btn btn--danger-solid", onclick: submit },
        [icon("#i-recycle", 15), "Empty Recycle"],
      ),
    ]),
  ]);
  openModal(el("div", { class: "modal" }, [body]));
}

function openMoveModal(items) {
  if (!items.length) return;

  const movingPaths = new Set(items.map((item) => item.path));
  let pickerPath = "";

  const crumbsRow = el("div", { class: "picker__crumbs" });
  const listRow = el("div", { class: "picker__list" });
  const destLabel = el("span", { class: "picker__dest" });
  const confirmButton = el("button", { type: "button", class: "btn btn--primary btn--modal", text: "Move here" });

  async function loadPicker(path) {
    pickerPath = path;
    const parts = pathParts(path);

    crumbsRow.textContent = "";
    const crumbs = [{ name: shareRootLabel(), path: "" }].concat(
      parts.map((name, index) => ({
        name: displayName(name),
        path: parts.slice(0, index + 1).join("/"),
      })),
    );
    crumbs.forEach((crumb, index) => {
      if (index > 0) crumbsRow.append(icon("#i-chev-r", 13));
      crumbsRow.append(el("button", { type: "button", text: crumb.name, onclick: () => loadPicker(crumb.path) }));
    });

    destLabel.textContent = `${shareRootLabel()}${parts.length ? ` / ${parts.map(displayName).join(" / ")}` : ""}`;
    // Moving something into itself or into the folder it already lives in is a no-op.
    confirmButton.disabled = path === state.path || movingPaths.has(path);

    listRow.textContent = "";
    listRow.append(el("div", { class: "picker__empty" }, [icon("#i-refresh", 18), "Loading folders…"]));

    let folders = [];
    try {
      const data = await api.listFiles(path);
      folders = (data.items || []).filter((item) => item.is_dir && !movingPaths.has(item.path));
    } catch (error) {
      listRow.textContent = "";
      listRow.append(el("div", { class: "picker__empty" }, [icon("#i-lock", 20), error.message]));
      return;
    }

    listRow.textContent = "";
    if (!folders.length) {
      listRow.append(
        el("div", { class: "picker__empty" }, [icon("#i-folder", 20), "No subfolders here — drop the files at this level."]),
      );
      return;
    }
    for (const folder of folders) {
      listRow.append(
        el("button", { type: "button", class: "picker__row", onclick: () => loadPicker(folder.path) }, [
          icon("#i-folder", 16),
          el("span", { text: displayName(folder.name) }),
          icon("#i-chev-r", 14),
        ]),
      );
    }
  }

  confirmButton.addEventListener("click", async () => {
    const dest = pickerPath;
    closeModal();
    await movePathsTo(
      items.map((item) => item.path),
      dest,
    );
  });

  const modal = el("div", { class: "modal modal--wide" }, [
    el("div", {}, [
      el("div", { class: "picker__head" }, [
        el("h3", { text: items.length === 1 ? `Move “${items[0].name}”` : `Move ${items.length} items` }),
        el("p", { text: "Pick a destination folder. Your NAS permissions are checked on drop." }),
      ]),
      crumbsRow,
      listRow,
      el("div", { class: "picker__foot" }, [
        destLabel,
        el("div", {}, [
          el("button", { type: "button", class: "btn btn--modal", text: "Cancel", onclick: closeModal }),
          confirmButton,
        ]),
      ]),
    ]),
  ]);

  openModal(modal);
  loadPicker(pathParts(state.path).slice(0, -1).join("/"));
}

/* ---------------------------------------------------------------------------
   Drive speed test (tunnel + NAS path as the browser sees it)
   --------------------------------------------------------------------------- */
const SPEEDTEST_SIZES = [
  { label: "32 MB", bytes: 32 * 1024 * 1024 },
  { label: "64 MB", bytes: 64 * 1024 * 1024 },
  { label: "128 MB", bytes: 128 * 1024 * 1024 },
  { label: "256 MB", bytes: 256 * 1024 * 1024 },
  { label: "512 MB", bytes: 512 * 1024 * 1024 },
];

function formatMbps(mbps) {
  if (mbps == null || !Number.isFinite(mbps)) return "—";
  if (mbps >= 100) return `${mbps.toFixed(0)} Mbps`;
  if (mbps >= 10) return `${mbps.toFixed(1)} Mbps`;
  return `${mbps.toFixed(2)} Mbps`;
}

function formatDuration(sec) {
  if (sec == null || !Number.isFinite(sec)) return "";
  return sec < 1 ? `${Math.round(sec * 1000)} ms` : `${sec.toFixed(2)} s`;
}

async function copyText(value, button) {
  const text = String(value || "");
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    if (button) {
      const prev = button.textContent;
      button.textContent = "Copied";
      setTimeout(() => {
        button.textContent = prev;
      }, 1400);
    }
  } catch {
    window.prompt("Copy this value:", text);
  }
}

const MAC_APP_ZIP = "https://github.com/neelsatyavolu/ugreen-drive/releases/latest/download/Ugreen-Drive-mac.zip";

function openFinderConnectModal() {
  const nd = state.me?.network_drive || {};
  const host = nd.host || "(your NAS IP)";
  const serverUrl = nd.server_url || `smb://${host}`;
  const nasUser = state.me?.nas_username || "(your NAS username)";

  function copyRow(label, value, hint) {
    const btn = el("button", {
      type: "button",
      class: "btn btn--modal finder-copy",
      text: "Copy",
    });
    btn.addEventListener("click", () => copyText(value, btn));
    return el("div", { class: "finder-row" }, [
      el("div", { class: "finder-row__meta" }, [
        el("div", { class: "finder-row__label", text: label }),
        el("code", { class: "mono finder-row__value", text: value }),
        hint ? el("div", { class: "finder-row__hint", text: hint }) : null,
      ].filter(Boolean)),
      btn,
    ]);
  }

  const installCmd = `curl -fsSL ${location.origin}/mac/install.sh | sh`;

  const body = el("div", { class: "modal__body" }, [
    el("div", { class: "modal__lead" }, [
      el("div", { class: "modal__badge" }, [icon("#i-hdd", 17)]),
      el("div", {}, [
        el("h3", { text: "Connect in Finder" }),
        el("p", {
          text: "The Mac app puts the drive in Finder at home and away, keeps it mounted and reconnects by itself. It lives in the menu bar.",
        }),
      ]),
    ]),

    copyRow("Install the Mac app", installCmd, "Paste in Terminal. It installs to Applications and opens the app; sign in with your NAS account. Needs macOS 13 or later."),
    el("p", { class: "finder-row__hint" }, [
      el("a", { href: MAC_APP_ZIP, target: "_blank", rel: "noopener", text: "Download the .zip instead" }),
      document.createTextNode(" (then enter this drive's address in the app)."),
    ]),

    el("h4", { class: "finder-sub", text: "Without the app (home network only)" }),
    el("ol", { class: "finder-steps" }, [
      el("li", { text: "Finder → Go → Connect to Server… (⌘K). On Windows: File Explorer → This PC → Map network drive." }),
      el("li", { text: "Paste the server address below, then pick a shared folder." }),
      el("li", {}, [
        document.createTextNode("Sign in as "),
        el("code", { class: "mono", text: nasUser }),
        document.createTextNode(" with your NAS password."),
      ]),
    ]),

    copyRow("Server", serverUrl, "Works only on the NAS's own network. This web address isn't a Connect to Server address."),

    modalFooter([
      el("button", { type: "button", class: "btn btn--modal", text: "Close", onclick: closeModal }),
    ]),
  ]);

  openModal(el("div", { class: "modal modal--finder" }, [body]));
}

const SPEEDTEST_MODES = [
  { id: "both", label: "Both" },
  { id: "download", label: "Download only" },
  { id: "upload", label: "Upload only" },
];

function openSpeedTestModal() {
  let sizeBytes = SPEEDTEST_SIZES[0].bytes;
  let mode = "both";
  let running = false;
  let abortUpload = null;

  const dlValue = el("div", { class: "speedtest__value is-muted", text: "—" });
  const ulValue = el("div", { class: "speedtest__value is-muted", text: "—" });
  const pingValue = el("div", { class: "speedtest__value is-muted", text: "—" });
  const dlSub = el("div", { class: "speedtest__sub", text: "NAS → your device" });
  const ulSub = el("div", { class: "speedtest__sub", text: "Your device → NAS" });
  const pingSub = el("div", { class: "speedtest__sub", text: "Round-trip to API" });
  const dlBar = el("div", { class: "speedtest__bar" });
  const ulBar = el("div", { class: "speedtest__bar" });
  const status = el("p", { class: "speedtest__note", text: "" });

  const sizeButtons = SPEEDTEST_SIZES.map((opt) =>
    el(
      "button",
      {
        type: "button",
        text: opt.label,
        "aria-pressed": String(opt.bytes === sizeBytes),
        onclick: () => {
          if (running) return;
          sizeBytes = opt.bytes;
          for (const btn of sizeButtons) {
            btn.setAttribute("aria-pressed", String(btn.textContent === opt.label));
          }
        },
      },
    ),
  );

  const modeButtons = SPEEDTEST_MODES.map((opt) =>
    el(
      "button",
      {
        type: "button",
        text: opt.label,
        "aria-pressed": String(opt.id === mode),
        onclick: () => {
          if (running) return;
          mode = opt.id;
          for (let i = 0; i < modeButtons.length; i++) {
            modeButtons[i].setAttribute(
              "aria-pressed",
              String(SPEEDTEST_MODES[i].id === mode),
            );
          }
        },
      },
    ),
  );

  const runBtn = el("button", {
    type: "button",
    class: "btn btn--primary btn--modal",
    text: "Run test",
  });
  const closeBtn = el("button", {
    type: "button",
    class: "btn btn--modal",
    text: "Close",
    onclick: () => {
      if (abortUpload) abortUpload();
      closeModal();
    },
  });

  function setIdleValues() {
    dlValue.className = "speedtest__value is-muted";
    ulValue.className = "speedtest__value is-muted";
    pingValue.className = "speedtest__value is-muted";
    dlValue.textContent = "—";
    ulValue.textContent = "—";
    pingValue.textContent = "—";
    dlSub.textContent = "NAS → your device";
    ulSub.textContent = "Your device → NAS";
    pingSub.textContent = "Round-trip to API";
    dlBar.style.width = "0";
    ulBar.style.width = "0";
    dlBar.classList.remove("is-indeterminate");
    ulBar.classList.remove("is-indeterminate");
  }

  function setControlsDisabled(disabled) {
    for (const btn of sizeButtons) btn.disabled = disabled;
    for (const btn of modeButtons) btn.disabled = disabled;
  }

  async function measurePing() {
    const samples = [];
    for (let i = 0; i < 3; i++) {
      const t0 = performance.now();
      const res = await fetch("/api/health", { credentials: "same-origin", cache: "no-store" });
      if (!res.ok) throw new Error("Health check failed");
      await res.json();
      samples.push(performance.now() - t0);
    }
    samples.sort((a, b) => a - b);
    return samples[1]; // median of 3
  }

  async function measureDownload(bytes, onProgress) {
    // Parallel streams saturate the Cloudflare Tunnel better than one connection.
    return api.speedtestDownloadMulti(bytes, onProgress);
  }

  async function measureUpload(bytes, onProgress) {
    // Chunked multi-stream upload — avoids single 512MB XHR dying near the end.
    const { promise, abort } = api.speedtestUploadMulti(bytes, onProgress);
    abortUpload = abort;
    try {
      return await promise;
    } finally {
      abortUpload = null;
    }
  }

  runBtn.addEventListener("click", async () => {
    if (running) return;
    running = true;
    runBtn.disabled = true;
    runBtn.textContent = "Testing…";
    setIdleValues();
    setControlsDisabled(true);

    const doDownload = mode === "both" || mode === "download";
    const doUpload = mode === "both" || mode === "upload";

    try {
      status.textContent = "Checking latency…";
      pingValue.className = "speedtest__value is-muted";
      pingValue.textContent = "…";
      const pingMs = await measurePing();
      pingValue.className = "speedtest__value";
      pingValue.textContent = `${Math.round(pingMs)} ms`;
      pingSub.textContent = "Median of 3 probes";

      if (doDownload) {
        status.textContent = `Downloading ${formatSize(sizeBytes)}…`;
        dlValue.className = "speedtest__value is-muted";
        dlValue.textContent = "…";
        dlBar.classList.add("is-indeterminate");
        const dl = await measureDownload(sizeBytes, (p) => {
          dlBar.classList.remove("is-indeterminate");
          dlBar.style.width = `${Math.round(p * 100)}%`;
        });
        dlBar.style.width = "100%";
        dlValue.className = "speedtest__value is-good";
        dlValue.textContent = formatMbps(dl.mbps);
        dlSub.textContent = `${formatSize(dl.bytes)} in ${formatDuration(dl.sec)}`;
      } else {
        dlValue.textContent = "—";
        dlSub.textContent = "Skipped";
      }

      if (doUpload) {
        status.textContent = `Uploading ${formatSize(sizeBytes)}…`;
        ulValue.className = "speedtest__value is-muted";
        ulValue.textContent = "…";
        ulBar.style.width = "0";
        ulBar.classList.add("is-indeterminate");
        const ul = await measureUpload(sizeBytes, (p) => {
          ulBar.classList.remove("is-indeterminate");
          ulBar.style.width = `${Math.round(p * 100)}%`;
        });
        ulBar.style.width = "100%";
        ulValue.className = "speedtest__value is-good";
        ulValue.textContent = formatMbps(ul.mbps);
        ulSub.textContent = `${formatSize(ul.bytes)} in ${formatDuration(ul.sec)}`;
      } else {
        ulValue.textContent = "—";
        ulSub.textContent = "Skipped";
      }

      status.textContent = "Done.";
    } catch (error) {
      if (error instanceof ApiError && error.isAuth) {
        closeModal();
        showLogin();
        return;
      }
      status.textContent = error.message || "Speed test failed.";
      toast(error.message || "Speed test failed", "warn");
      // Keep any finished metrics visible (e.g. download OK, upload failed).
    } finally {
      running = false;
      abortUpload = null;
      runBtn.disabled = false;
      runBtn.textContent = "Run again";
      setControlsDisabled(false);
      dlBar.classList.remove("is-indeterminate");
      ulBar.classList.remove("is-indeterminate");
    }
  });

  const modal = el("div", { class: "modal modal--speedtest" }, [
    el("div", { class: "speedtest" }, [
      el("div", { class: "speedtest__lead" }, [
        el("h3", { text: "Drive speed test" }),
        el("p", {
          text: "Check how fast this browser can push and pull data through the drive.",
        }),
      ]),
      el("div", { class: "speedtest__modes" }, modeButtons),
      el("div", { class: "speedtest__sizes" }, sizeButtons),
      el("div", { class: "speedtest__rows" }, [
        el("div", { class: "speedtest__row" }, [
          icon("#i-refresh", 16),
          el("div", {}, [el("div", { class: "speedtest__label", text: "Latency" }), pingSub]),
          pingValue,
        ]),
        el("div", { class: "speedtest__row" }, [
          icon("#i-download", 16),
          el("div", {}, [el("div", { class: "speedtest__label", text: "Download" }), dlSub]),
          dlValue,
          el("div", { class: "speedtest__track" }, [dlBar]),
        ]),
        el("div", { class: "speedtest__row" }, [
          icon("#i-upload", 16),
          el("div", {}, [el("div", { class: "speedtest__label", text: "Upload" }), ulSub]),
          ulValue,
          el("div", { class: "speedtest__track" }, [ulBar]),
        ]),
      ]),
      status,
      modalFooter([closeBtn, runBtn]),
    ]),
  ]);

  openModal(modal);
}

/* ---------------------------------------------------------------------------
   Transfers (uploads + downloads) — shared tray, speed, ETA, cancel
   --------------------------------------------------------------------------- */
/** Rolling window + light EMA so MB/s doesn't thrash on every progress tick. */
const XFER_SPEED_WINDOW_MS = 2500;
const XFER_SPEED_MIN_MS = 450;
const XFER_SPEED_EMA = 0.3;

function noteTransferBytes(entry, loadedBytes) {
  const loaded = Math.max(0, Number(loadedBytes) || 0);
  entry.loadedBytes = loaded;
  const size = entry.size || 0;
  if (size > 0) {
    entry.progress = Math.max(0, Math.min(0.999, loaded / size));
  } else {
    // Unknown total (streaming zip without Content-Length): keep 0 and show bytes.
    entry.progress = 0;
  }
  const now = performance.now();

  if (!entry._speedHist) entry._speedHist = [];
  const hist = entry._speedHist;
  // Coalesce sub-100ms ticks so the window stays small and less noisy.
  const prev = hist[hist.length - 1];
  if (prev && now - prev.t < 100) {
    prev.t = now;
    prev.b = loaded;
  } else {
    hist.push({ t: now, b: loaded });
  }
  const cutoff = now - XFER_SPEED_WINDOW_MS;
  while (hist.length > 2 && hist[0].t < cutoff) hist.shift();

  if (hist.length < 2) return;
  const first = hist[0];
  const last = hist[hist.length - 1];
  const dtMs = last.t - first.t;
  if (dtMs < XFER_SPEED_MIN_MS) return;
  const instant = Math.max(0, (last.b - first.b) / (dtMs / 1000));
  entry.speedBps =
    entry.speedBps == null
      ? instant
      : entry.speedBps * (1 - XFER_SPEED_EMA) + instant * XFER_SPEED_EMA;
}

function noteUploadProgress(entry, fraction) {
  const clamped = Math.max(0, Math.min(1, Number(fraction) || 0));
  noteTransferBytes(entry, clamped * (entry.size || 0));
  entry.progress = clamped;
}

function noteDownloadProgress(entry, { loaded, total, lengthComputable } = {}) {
  if (lengthComputable && total > 0) {
    entry.size = total;
  }
  noteTransferBytes(entry, loaded);
}

let uploadRenderRaf = 0;
function scheduleRenderUploads() {
  if (uploadRenderRaf) return;
  uploadRenderRaf = requestAnimationFrame(() => {
    uploadRenderRaf = 0;
    renderUploads();
  });
}

function allTransfers() {
  return [...state.uploads, ...state.downloads];
}

function isTransferActive(entry) {
  return entry.status === "pending" || entry.status === "uploading" || entry.status === "downloading";
}

// Browser progress measures bytes sent, not the server's final save/assembly.
function isUploadProcessing(entry) {
  return entry.status === "uploading" && entry.progress >= 1;
}

/** Bytes still to send/receive for one queue entry (0 when finished / cancelled). */
function transferRemainingBytes(entry) {
  if (entry.status === "done" || entry.status === "cancelled" || entry.status === "error") return 0;
  const size = entry.size || 0;
  if (!size) return 0;
  if (entry.status === "pending") return size;
  const loaded = entry.loadedBytes != null ? entry.loadedBytes : size * (entry.progress || 0);
  return Math.max(0, size - loaded);
}

/** Bytes already accounted for (completed + partial progress on active/failed). */
function transferLoadedBytes(entry) {
  if (entry.status === "done") return entry.size || entry.loadedBytes || 0;
  if (
    entry.status === "uploading" ||
    entry.status === "downloading" ||
    entry.status === "cancelled" ||
    entry.status === "error"
  ) {
    if (entry.loadedBytes != null) return Math.max(0, entry.loadedBytes);
    return Math.max(0, (entry.size || 0) * (entry.progress || 0));
  }
  return 0;
}

// Rows are rebuilt on every progress frame; a negative delay locks the sweep to
// one clock so a fresh bar doesn't restart off-track (matches app.css 1.2s).
function transferSweepDelay() {
  return `-${Math.round(performance.now() % 1200)}ms`;
}

function renderUploads() {
  const tray = $("upload-tray");
  const transfers = allTransfers();
  if (!transfers.length) {
    show(tray, false);
    return;
  }
  show(tray, true);

  const hasUp = state.uploads.length > 0;
  const hasDown = state.downloads.length > 0;
  const done = transfers.filter((entry) => entry.status === "done").length;
  const failed = transfers.filter((entry) => entry.status === "error").length;
  const total = transfers.length;
  const finished = transfers.every((entry) => !isTransferActive(entry));
  const processing = transfers.filter(isUploadProcessing).length;
  const onlyProcessing = processing > 0 && transfers.every(
    (entry) => !isTransferActive(entry) || isUploadProcessing(entry),
  );
  const activeSpeed = transfers.reduce(
    (sum, entry) =>
      (entry.status === "uploading" || entry.status === "downloading") &&
      !isUploadProcessing(entry) && entry.speedBps
        ? sum + entry.speedBps
        : sum,
    0,
  );
  const totalBytes = transfers.reduce((sum, entry) => sum + (entry.size || 0), 0);
  const loadedBytes = transfers.reduce((sum, entry) => sum + transferLoadedBytes(entry), 0);
  const remainingBytes = transfers.reduce((sum, entry) => sum + transferRemainingBytes(entry), 0);
  const knownSizes = transfers.every((entry) => (entry.size || 0) > 0 || entry.status === "done");
  const overallFrac =
    totalBytes > 0 ? Math.min(1, loadedBytes / totalBytes) : finished ? 1 : 0;
  const overallPct = Math.round(overallFrac * 100);
  const speedLabel = !finished ? formatSpeed(activeSpeed) : null;
  const etaLabel =
    !finished && activeSpeed > 0 && remainingBytes > 0
      ? formatEta(remainingBytes / activeSpeed)
      : null;

  const trayIcon = $("upload-tray-icon");
  if (trayIcon) {
    trayIcon.setAttribute(
      "href",
      hasUp && hasDown ? "#i-upload" : hasDown ? "#i-download" : "#i-upload",
    );
  }

  let title;
  if (finished) {
    if (failed) {
      title = hasUp && hasDown
        ? `Finished ${done} of ${total}`
        : hasDown
          ? `Downloaded ${done} of ${total}`
          : `Uploaded ${done} of ${total}`;
    } else if (hasUp && hasDown) {
      title = "Transfers complete";
    } else if (hasDown) {
      title = total === 1 ? "Download complete" : "Downloads complete";
    } else {
      title = total === 1 ? "Upload complete" : "Upload complete";
    }
  } else if (onlyProcessing) {
    title = processing === 1 ? "Processing…" : `Processing ${processing} files…`;
  } else if (hasUp && hasDown) {
    title = knownSizes && totalBytes > 0 ? `Transfers · ${overallPct}%` : "Transfers";
  } else if (hasDown) {
    title =
      knownSizes && totalBytes > 0
        ? total === 1
          ? `Downloading · ${overallPct}%`
          : `Downloading ${total} · ${overallPct}%`
        : total === 1
          ? "Downloading…"
          : `Downloading ${total}…`;
  } else {
    title =
      total === 1
        ? `Uploading · ${overallPct}%`
        : `Uploading ${total} files · ${overallPct}%`;
  }
  $("upload-title").textContent = title;

  const footParts = [];
  if (totalBytes > 0) {
    footParts.push(
      finished
        ? formatSize(totalBytes)
        : `${formatSize(loadedBytes)} / ${formatSize(totalBytes)}`,
    );
  } else if (!finished && loadedBytes > 0) {
    footParts.push(formatSize(loadedBytes));
  }
  footParts.push(`${done} of ${total} done`);
  const skipped = transfers.filter((entry) => entry.skipped && entry.status === "done").length;
  if (skipped) footParts.push(`${skipped} unchanged · skipped`);
  if (processing) footParts.push(`${processing} processing`);
  if (speedLabel) footParts.push(speedLabel);
  if (etaLabel) footParts.push(`${etaLabel} left`);
  $("upload-foot").textContent = footParts.join(" · ");
  $("upload-cancel").textContent = finished ? "Dismiss" : "Cancel all";

  const overallBar = $("upload-overall-bar");
  const overallTrack = $("upload-overall");
  if (overallBar && overallTrack) {
    const indeterminate = onlyProcessing || (!finished && totalBytes <= 0);
    overallBar.style.width = indeterminate ? "40%" : `${finished ? 100 : overallPct}%`;
    overallBar.className = `uploads__overall-bar${finished && !failed ? " is-done" : ""}${
      failed && finished ? " is-error" : ""
    }${indeterminate ? " is-indeterminate" : ""}`;
    show(overallTrack, true);
  }

  const list = $("upload-list");
  list.textContent = "";
  const displayPriority = (entry) =>
    entry.status === "uploading" || entry.status === "downloading"
      ? 0
      : entry.status === "done" ? 2 : 1;
  const orderedTransfers = [...transfers].sort(
    (a, b) => displayPriority(a) - displayPriority(b),
  );
  for (const entry of orderedTransfers) {
    const isDl = entry.direction === "download";
    const processingFile = isUploadProcessing(entry);
    const active = !processingFile && (entry.status === "uploading" || entry.status === "downloading");
    const percent = Math.round((entry.progress || 0) * 100);
    const size = entry.size || 0;
    const loaded = transferLoadedBytes(entry);
    const fileSpeed = active ? formatSpeed(entry.speedBps) : null;
    const fileEta =
      active && entry.speedBps > 0 && size > 0
        ? formatEta(transferRemainingBytes(entry) / entry.speedBps)
        : null;

    let statusText;
    let detailText;
    if (entry.status === "done") {
      statusText = entry.skipped ? "Skipped" : entry.native ? "Started" : "Done";
      detailText = entry.skipped ? "Unchanged · already exists" : entry.native ? "Browser downloads" : formatSize(size || loaded);
    } else if (entry.status === "error") {
      statusText = "Failed";
      detailText = entry.error || (isDl ? "Download failed" : "Upload failed");
    } else if (entry.status === "cancelled") {
      statusText = "Cancelled";
      detailText = size ? formatSize(size) : formatSize(loaded) || "—";
    } else if (entry.checking) {
      statusText = "Checking…";
      detailText = "Comparing with existing file";
    } else if (processingFile) {
      statusText = "Processing…";
      detailText = "Upload sent · saving file…";
    } else if (entry.status === "pending") {
      statusText = "Queued";
      detailText = size ? formatSize(size) : entry.detailLabel || "—";
    } else {
      statusText = size > 0 ? `${percent}%` : "…";
      const bits = [];
      if (size > 0) bits.push(`${formatSize(loaded)} / ${formatSize(size)}`);
      else if (loaded > 0) bits.push(formatSize(loaded));
      if (fileSpeed) bits.push(fileSpeed);
      if (fileEta) bits.push(`${fileEta} left`);
      detailText = bits.length ? bits.join(" · ") : size ? formatSize(size) : "Working…";
    }

    const indeterminate = entry.checking || processingFile || (active && size <= 0);
    const bar = el("div", {
      class: `upload__bar${entry.status === "done" ? " is-done" : ""}${
        entry.status === "error" ? " is-error" : ""
      }${indeterminate ? " is-indeterminate" : ""}`,
    });
    bar.style.width = entry.status === "done" ? "100%" : indeterminate ? "40%" : `${percent}%`;
    if (indeterminate) bar.style.animationDelay = transferSweepDelay();

    const canCancelOne = isTransferActive(entry);
    const rowActions = canCancelOne
      ? el(
          "button",
          {
            type: "button",
            class: "upload__cancel",
            "aria-label": `Cancel ${entry.name}`,
            title: "Cancel",
            onclick: (event) => {
              event.stopPropagation();
              cancelOneTransfer(entry);
            },
          },
          [icon("#i-x", 12)],
        )
      : el("span", {
          class: `upload__status${entry.status === "error" ? " is-error" : ""}${
            entry.status === "done" ? " is-done" : ""
          }`,
          text: statusText,
          title: statusText,
        });

    const kindIcon = entry.kind?.icon || (isDl ? "#i-download" : "#i-upload");
    const kindColor = entry.kind?.color || "hsl(var(--muted-foreground))";

    list.append(
      el("div", { class: "upload" }, [
        el("div", { class: "upload__top" }, [
          icon(kindIcon, 14, `color:${kindColor};flex:0 0 auto`),
          el("div", { class: "upload__meta" }, [
            el("span", { class: "upload__name", text: entry.name, title: entry.name }),
            el("span", {
              class: `upload__detail${entry.status === "error" ? " is-error" : ""}`,
              text: detailText,
              title: detailText,
            }),
          ]),
          rowActions,
        ]),
        el("div", { class: "upload__track" }, [bar]),
      ]),
    );
  }
}

function cancelOneTransfer(entry) {
  if (!entry) return;
  if (entry.status === "pending") {
    entry.status = "cancelled";
    entry.speedBps = null;
    entry._speedHist = null;
    renderUploads();
    return;
  }
  if ((entry.status === "uploading" || entry.status === "downloading") && entry.abort) {
    entry.abort();
    // Status is set in the runner's catch path; force a paint in case abort is sync-noop.
    renderUploads();
  }
}

/** @deprecated alias — older call sites */
function cancelOneUpload(entry) {
  cancelOneTransfer(entry);
}

function queueDownload({ name, filename, url, size = 0, kind, detailLabel, saveHandlePromise, parallel = false }) {
  const entry = {
    direction: "download",
    name: name || filename || "download",
    filename: filename || name || "download",
    url,
    size: Math.max(0, Number(size) || 0),
    kind: kind || { icon: "#i-download", color: "hsl(var(--muted-foreground))", label: "File" },
    detailLabel: detailLabel || null,
    saveHandlePromise: saveHandlePromise || null,
    parallel: Boolean(parallel),
    progress: 0,
    loadedBytes: 0,
    status: "pending",
    error: null,
    abort: null,
    speedBps: null,
    _speedHist: null,
  };
  state.downloads = state.downloads
    .filter((e) => e.status === "downloading" || e.status === "pending")
    .concat(entry);
  renderUploads();
  void pumpDownloads();
}

async function pumpDownloads() {
  if (state.downloadRunning) return;
  state.downloadRunning = true;

  try {
    for (;;) {
      const next = state.downloads.find((e) => e.status === "pending");
      if (!next) break;

      next.status = "downloading";
      next.speedBps = null;
      next._speedHist = null;
      next.loadedBytes = 0;
      next.progress = 0;
      renderUploads();

      const { promise, abort } = api.downloadTransfer(next.url, {
        filename: next.filename,
        saveHandlePromise: next.saveHandlePromise,
        parallel: next.parallel,
        size: next.size,
        onProgress: (info) => {
          noteDownloadProgress(next, info);
          scheduleRenderUploads();
        },
      });
      next.abort = abort;

      try {
        const result = await promise;
        next.status = "done";
        next.progress = 1;
        if (result?.native) {
          next.native = true;
          toast("Download started in your browser", "ok");
        }
        if (result?.size) {
          next.size = result.size;
          next.loadedBytes = result.size;
        } else {
          next.loadedBytes = next.size || next.loadedBytes || 0;
        }
        next.speedBps = null;
        next._speedHist = null;
      } catch (error) {
        next.status = error?.message === "Download cancelled." ? "cancelled" : "error";
        next.error = error?.message || "Download failed";
        next.speedBps = null;
        next._speedHist = null;
        if (error instanceof ApiError && error.isAuth) {
          showLogin();
        }
      }
      next.abort = null;
      renderUploads();
    }
  } finally {
    state.downloadRunning = false;
    // Another download may have been queued while the last one finished.
    if (state.downloads.some((e) => e.status === "pending")) {
      void pumpDownloads();
    } else {
      const failed = state.downloads.filter((e) => e.status === "error");
      if (failed.length) {
        toast(`${failed.length} download${failed.length === 1 ? "" : "s"} failed`, "warn");
      }
    }
  }
}

/** Join relative path segments under the share (no leading/trailing slashes). */
function joinRelPath(...parts) {
  return parts
    .flatMap((p) => String(p || "").replace(/\\/g, "/").split("/"))
    .filter((s) => s && s !== "." && s !== "..")
    .join("/");
}

/**
 * Queue FileList / File[] for upload. Folder picks and folder drops set
 * webkitRelativePath so nested structure is preserved under the current folder.
 * Uses multi-file concurrency + chunked multi-stream for large files (api.uploadFile).
 */
/** True when `dest` is the folder on screen, or nested inside it. */
function destinationInView(dest) {
  const viewing = state.path || "";
  if (!viewing) return true; // share root: every upload lands under the listing
  return dest === viewing || dest.startsWith(`${viewing}/`);
}

/**
 * Reload the open folder shortly after an upload lands, so files appear
 * without a manual refresh. Coalesced so a burst of completions in a large
 * batch costs one listing call, not one per file.
 */
let uploadRefreshTimer = 0;
function scheduleListingRefresh() {
  clearTimeout(uploadRefreshTimer);
  uploadRefreshTimer = setTimeout(async () => {
    uploadRefreshTimer = 0;
    // loadFolder resets selection; the user didn't ask for that mid-upload.
    const keep = [...state.selection];
    await loadFolder(state.path);
    if (!keep.length) return;
    const present = new Set(state.items.map((entry) => entry.path));
    for (const path of keep) {
      if (present.has(path)) state.selection.add(path);
    }
    if (state.selection.size) render();
  }, 600);
}

function promptFolderUploadConflict(name) {
  return new Promise((resolve) => {
    const choose = (choice) => {
      resolve(choice);
      closeModal();
    };
    const body = el("div", { class: "modal__body" }, [
      el("h3", { text: `“${name}” already exists` }),
      el("p", { text: "Merge skips unchanged files, uploads new or changed files, and keeps other existing contents. Replace removes the existing folder and all its contents before uploading. If the upload fails, the old contents will already have been removed." }),
      modalFooter([
        el("button", { type: "button", class: "btn btn--modal", text: "Cancel", onclick: () => choose(null) }),
        el("button", { type: "button", class: "btn btn--danger btn--modal", text: "Replace", onclick: () => choose("replace") }),
        el("button", { type: "button", class: "btn btn--primary btn--modal", text: "Merge", onclick: () => choose("merge") }),
      ]),
    ]);
    openModal(el("div", { class: "modal" }, [body]), { onClose: () => resolve(null) });
  });
}

async function prepareFolderUploads(queued, basePath, share) {
  if (state.share !== share) return false;
  const roots = [...new Set(queued.filter((entry) => entry.name.includes("/")).map((entry) => entry.name.split("/")[0]))];
  if (!roots.length) return true;
  const { items } = await api.listFiles(basePath);
  const replacements = [];
  for (const name of roots) {
    if (state.share !== share) return false;
    const existing = items.find((item) => item.name === name);
    if (!existing) continue;
    if (!existing.is_dir) throw new Error(`A file named “${name}” already exists. Rename it before uploading this folder.`);
    const choice = await promptFolderUploadConflict(name);
    if (!choice || state.share !== share) return false;
    if (choice === "replace") replacements.push(joinRelPath(basePath, name));
    if (choice === "merge") {
      for (const entry of queued) {
        if (entry.name.startsWith(`${name}/`)) entry.merge = true;
      }
    }
  }
  // Collect every choice before removing anything: Cancel leaves the batch untouched.
  for (const path of replacements) {
    if (state.uploads.some((entry) =>
      (entry.status === "pending" || entry.status === "uploading") &&
      (entry.targetPath === path || entry.targetPath?.startsWith(`${path}/`)))) {
      throw new Error("Wait for uploads into this folder to finish before replacing it.");
    }
  }
  for (const path of replacements) {
    if (state.share !== share) return false;
    await api.deleteItem(path);
  }
  return state.share === share;
}

// Keep simultaneous folder picks/drops from replacing each other's prompts.
let uploadPreparation = Promise.resolve();

async function startUploads(files) {
  if (!files.length) return;
  const basePath = state.path;
  const share = state.share;

  const queued = [...files].map((file) => {
    const rel = String(file.webkitRelativePath || "").replace(/\\/g, "/");
    const segments = rel.split("/").filter((s) => s && s !== "." && s !== "..");
    const fileName = segments.length ? segments[segments.length - 1] : file.name;
    const subDir = segments.length > 1 ? segments.slice(0, -1).join("/") : "";
    const targetPath = joinRelPath(basePath, subDir);
    const displayName = segments.length ? segments.join("/") : file.name;
    return {
      direction: "upload",
      file,
      name: displayName,
      size: file.size,
      kind: describeKind({ name: fileName, is_dir: false }),
      progress: 0,
      loadedBytes: 0,
      status: "pending",
      error: null,
      abort: null,
      targetPath,
      basePath,
      share,
      speedBps: null,
      _speedHist: null,
    };
  });

  const preparation = uploadPreparation.then(() => prepareFolderUploads(queued, basePath, share));
  uploadPreparation = preparation.catch(() => {});
  try {
    if (!await preparation) return;
  } catch (error) {
    handleMutationError(error);
    return;
  }

  state.uploads = state.uploads.filter((entry) => entry.status === "uploading" || entry.status === "pending").concat(queued);
  renderUploads();

  if (state.uploadRunning) return;
  state.uploadRunning = true;

  // Parallel multi-file upload — each large file also multi-streams chunks.
  const concurrency = api.UPLOAD_FILE_CONCURRENCY || 4;
  let authLost = false;

  function claimNext() {
    const next = state.uploads.find((e) => e.status === "pending");
    if (!next) return null;
    next.status = "uploading";
    next.speedBps = null;
    next._speedHist = null;
    return next;
  }

  async function runOne(entry) {
    if (authLost) return;
    renderUploads();

    const dest = entry.targetPath != null ? entry.targetPath : basePath;
    try {
      if (state.share !== entry.share) throw new Error("The active drive changed. Start this upload again.");
      if (entry.merge) {
        const controller = new AbortController();
        entry.abort = () => controller.abort();
        entry.checking = true;
        renderUploads();
        try {
          entry.skipped = await api.uploadFileUnchanged(joinRelPath(dest, entry.file.name), entry.file, controller.signal);
        } catch (error) {
          if (controller.signal.aborted) throw new Error("Upload cancelled.");
          throw error;
        } finally {
          entry.checking = false;
        }
        if (controller.signal.aborted) throw new Error("Upload cancelled.");
      }
      if (!entry.skipped) {
        if (state.share !== entry.share) throw new Error("The active drive changed. Start this upload again.");
        const { promise, abort } = api.uploadFile(dest, entry.file, (fraction) => {
          noteUploadProgress(entry, fraction);
          scheduleRenderUploads();
        });
        entry.abort = abort;
        renderUploads();
        await promise;
      }
      entry.status = "done";
      entry.progress = 1;
      entry.loadedBytes = entry.size || 0;
      entry.speedBps = null;
      entry._speedHist = null;
      // Show it in the listing now rather than waiting for the whole batch.
      if (!entry.skipped && destinationInView(dest)) scheduleListingRefresh();
    } catch (error) {
      entry.status = error.message === "Upload cancelled." || error.name === "AbortError" ? "cancelled" : "error";
      entry.error = error.message;
      entry.speedBps = null;
      entry._speedHist = null;
      if (error instanceof ApiError && error.isAuth) {
        authLost = true;
        showLogin();
      }
    }
    entry.abort = null;
    renderUploads();
  }

  async function worker() {
    for (;;) {
      if (authLost) return;
      const next = claimNext();
      if (!next) return;
      await runOne(next);
    }
  }

  // Loop rather than a single Promise.all: a batch queued while the workers
  // were draining would otherwise sit at "pending" with nobody left to run it.
  do {
    await Promise.all(Array.from({ length: concurrency }, () => worker()));
  } while (!authLost && state.uploads.some((entry) => entry.status === "pending"));

  state.uploadRunning = false;
  if (authLost) return;

  const failed = state.uploads.filter((entry) => entry.status === "error");
  if (failed.length) toast(`${failed.length} upload${failed.length === 1 ? "" : "s"} failed`, "warn");

  // Final settle: catches the tail of the batch and any nested folder uploads.
  if (
    state.uploads.some(
      (entry) => entry.status === "done" && destinationInView(entry.targetPath != null ? entry.targetPath : entry.basePath || ""),
    )
  ) {
    clearTimeout(uploadRefreshTimer);
    uploadRefreshTimer = 0;
    await loadFolder(state.path);
  }
}

function closeUploadMenu() {
  const menu = document.getElementById("upload-menu");
  if (menu) menu.remove();
  const btn = $("btn-upload");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

function closeSortMenu() {
  const menu = document.getElementById("sort-menu");
  if (menu) menu.remove();
  const btn = $("btn-sort-by");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

function openSortMenu() {
  const anchor = $("btn-sort-by");
  if (!anchor) return;
  if (document.getElementById("sort-menu")) {
    closeSortMenu();
    return;
  }
  closeUploadMenu();
  closeCrumbOverflow();
  state.openMenu = null;
  state.menuAnchor = null;
  clearPortaledMenus();

  const menu = el("div", {
    class: "menu menu--portal",
    id: "sort-menu",
    role: "menu",
    onclick: (event) => event.stopPropagation(),
  });
  for (const key of ["name", "kind", "size", "mtime"]) {
    const active = state.sortKey === key;
    const label = SORT_LABELS[key];
    const kids = [];
    if (active) kids.push(icon(state.sortAsc ? "#i-arrow-u" : "#i-chev-d", 14));
    else kids.push(el("span", { class: "menu__spacer", style: "width:14px;flex:0 0 auto" }));
    kids.push(label);
    menu.append(
      el(
        "button",
        {
          type: "button",
          role: "menuitem",
          class: active ? "is-active" : "",
          onclick: () => {
            closeSortMenu();
            setSort(key);
          },
        },
        kids,
      ),
    );
  }
  document.body.append(menu);
  const rect = anchor.getBoundingClientRect();
  placeFixedMenu(menu, rect.bottom + 4, rect.right - (menu.offsetWidth || 160));
  anchor.setAttribute("aria-expanded", "true");
}

function openShortcutsModal() {
  const apple = isApplePlatform();
  const modK = apple ? "⌘K" : "Ctrl+K";
  const modA = apple ? "⌘A" : "Ctrl+A";
  const modC = apple ? "⌘C" : "Ctrl+C";
  const modN = apple ? "⌘⇧N" : "Ctrl+Shift+N";
  const rows = [
    [`${modK}  or  /`, "Search files and folders"],
    ["Type a name", "Jump to a matching item"],
    ["↑ ↓ ← →", "Move the cursor"],
    ["Shift + arrows", "Extend the selection"],
    [modA, "Select all"],
    [modC, "Copy selected path"],
    ["Enter", "Open folder or file"],
    ["Space", "Preview media"],
    ["F2", "Rename"],
    [modN, "New folder"],
    ["Delete", "Move to Recycle"],
    ["Esc", "Clear selection / close"],
    ["?", "Keyboard shortcuts"],
  ];
  const list = el("div", { class: "keys" });
  for (const [combo, label] of rows) {
    list.append(
      el("div", { class: "keys__row" }, [
        el("kbd", { text: combo }),
        el("span", { text: label }),
      ]),
    );
  }
  const body = el("div", { class: "modal__body" }, [
    el("h3", { text: "Keyboard shortcuts" }),
    el("p", { text: "These work anywhere in the file list — not while typing in a field." }),
    list,
    modalFooter([
      el("button", { type: "button", class: "btn btn--primary", text: "Done", onclick: closeModal }),
    ]),
  ]);
  openModal(el("div", { class: "modal modal--keys" }, [body]));
}

/** Split Upload into file picker vs folder picker (webkitdirectory). */
function openUploadMenu(anchor) {
  if (!anchor) return;
  const existing = document.getElementById("upload-menu");
  if (existing) {
    closeUploadMenu();
    return;
  }
  clearPortaledMenus();
  state.openMenu = null;
  state.menuAnchor = null;

  const menu = el("div", {
    class: "menu menu--portal",
    id: "upload-menu",
    role: "menu",
    onclick: (event) => event.stopPropagation(),
  });

  const items = [
    {
      label: "Upload files",
      symbol: "#i-file",
      run: () => $("file-input").click(),
    },
    {
      label: "Upload folder",
      symbol: "#i-folder",
      run: () => $("folder-input").click(),
    },
  ];

  for (const entry of items) {
    menu.append(
      el(
        "button",
        {
          type: "button",
          role: "menuitem",
          onclick: () => {
            closeUploadMenu();
            entry.run();
          },
        },
        [icon(entry.symbol, 14), entry.label],
      ),
    );
  }

  document.body.append(menu);
  const rect = anchor.getBoundingClientRect();
  placeFixedMenu(menu, rect.bottom + 4, rect.left);
  if (anchor.id === "btn-upload") anchor.setAttribute("aria-expanded", "true");
}

function cancelUploads() {
  const transfers = allTransfers();
  const active = transfers.some((entry) => isTransferActive(entry));
  if (active) {
    for (const entry of transfers) {
      if (entry.status === "pending") entry.status = "cancelled";
      if ((entry.status === "uploading" || entry.status === "downloading") && entry.abort) {
        entry.abort();
      }
    }
    renderUploads();
    return;
  }
  state.uploads = [];
  state.downloads = [];
  renderUploads();
}

/* ---------------------------------------------------------------------------
   Drawer (mobile)
   --------------------------------------------------------------------------- */
function closeDrawer() {
  $("screen-browser").dataset.drawer = "closed";
}

/* ---------------------------------------------------------------------------
   Wiring
   --------------------------------------------------------------------------- */
function bindEvents() {
  const hint = $("search-hint");
  if (hint) {
    const combo = isApplePlatform() ? "⌘K" : "Ctrl+K";
    hint.textContent = combo;
    hint.title = `Press ${combo} or / to search`;
  }

  $("btn-up").addEventListener("click", () => {
    const parts = pathParts(state.path);
    if (parts.length) navigate(parts.slice(0, -1).join("/"));
  });

  $("btn-refresh").addEventListener("click", () => {
    const glyph = $("refresh-icon");
    glyph.classList.remove("spinning");
    void glyph.offsetWidth; // restart the CSS animation
    glyph.classList.add("spinning");
    loadFolder(state.path);
    refreshShortcuts();
    loadUsage();
  });

  $("btn-theme").addEventListener("click", () => {
    applyTheme(document.documentElement.classList.contains("light") ? "dark" : "light");
  });

  // bfcache / multi-tab: re-sync if storage or restored snapshot drifted.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) applyTheme(storedTheme());
  });
  window.addEventListener("storage", (event) => {
    if (event.key === THEME_KEY) applyTheme(storedTheme());
  });

  $("btn-speedtest")?.addEventListener("click", () => openSpeedTestModal());
  $("btn-settings")?.addEventListener("click", () =>
    openSettingsModal({
      openModal,
      closeModal,
      modalFooter,
      toast,
      shares: state.shares,
      onSaved: (saved) => applyDriveName(saved?.values?.drive_name),
    }),
  );
  $("btn-finder")?.addEventListener("click", () => openFinderConnectModal());

  const searchInput = $("search");
  searchInput.addEventListener("input", (event) => {
    state.query = event.target.value;
    state.selection.clear();
    state.openMenu = null;
    // Reset scope to current folder when starting a new query from empty.
    if (!state.query.trim()) {
      searchUi.scope = "here";
      closeSearchPanel();
    } else {
      searchUi.open = true;
      runSearch();
    }
    render();
  });
  searchInput.addEventListener("focus", () => {
    if (state.query.trim()) {
      searchUi.open = true;
      if (!searchUi.results.length && !searchUi.loading) runSearch(true);
      else renderSearchPanel();
    }
  });
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      if (!searchUi.open && state.query.trim()) {
        searchUi.open = true;
        renderSearchPanel();
      }
      if (searchUi.open && searchUi.results.length) {
        event.preventDefault();
        moveSearchActive(1);
      }
    } else if (event.key === "ArrowUp") {
      if (searchUi.open && searchUi.results.length) {
        event.preventDefault();
        moveSearchActive(-1);
      }
    } else if (event.key === "Enter") {
      if (searchUi.open && searchUi.results.length && searchUi.active >= 0) {
        event.preventDefault();
        const item = searchUi.results[searchUi.active];
        void openSearchResult(item, event.altKey || event.metaKey);
      }
    } else if (event.key === "Escape") {
      if (searchUi.open) {
        event.preventDefault();
        event.stopPropagation();
        closeSearchPanel();
      } else if (state.query) {
        event.preventDefault();
        clearSearch();
        render();
      }
    } else if (event.key === "Tab") {
      closeSearchPanel();
    }
  });

  $("btn-upload").addEventListener("click", (event) => {
    event.stopPropagation();
    openUploadMenu($("btn-upload"));
  });
  $("btn-new-folder").addEventListener("click", openNewFolderModal);

  function onPickFiles(event) {
    const files = [...(event.target.files || [])];
    event.target.value = "";
    startUploads(files);
  }
  $("file-input").addEventListener("change", onPickFiles);
  $("folder-input").addEventListener("change", onPickFiles);

  $("btn-download").addEventListener("click", () => downloadItems(selectedItems()));
  $("btn-copy-link").addEventListener("click", () => copyItemLinks(selectedItems()));
  $("btn-copy-path")?.addEventListener("click", () => copyItemPaths(selectedItems()));
  $("btn-rename").addEventListener("click", () => {
    const [item] = selectedItems();
    if (item) openRenameModal(item);
  });
  $("btn-move").addEventListener("click", () => openMoveModal(selectedItems()));
  $("btn-delete").addEventListener("click", () => openDeleteModal(selectedItems()));
  $("btn-empty-recycle")?.addEventListener("click", () => openEmptyRecycleModal());
  $("btn-clear-sel").addEventListener("click", () => {
    state.selection.clear();
    render();
  });

  $("check-all").addEventListener("click", () => {
    const visible = visibleItems();
    const allSelected = visible.length > 0 && visible.every((item) => state.selection.has(item.path));
    state.selection = allSelected ? new Set() : new Set(visible.map((item) => item.path));
    render();
  });

  $("btn-sort-by")?.addEventListener("click", (event) => {
    event.stopPropagation();
    openSortMenu();
  });
  $("rows")?.querySelector(".rows__head")?.addEventListener("click", (event) => {
    const btn = event.target.closest(".sort[data-sort]");
    if (!btn) return;
    setSort(btn.dataset.sort);
  });

  $("btn-density").addEventListener("click", () => {
    state.density = state.density === "compact" ? "comfortable" : "compact";
    store(DENSITY_KEY, state.density);
    render();
  });

  $("btn-hidden").addEventListener("click", () => {
    state.hideDotfiles = !state.hideDotfiles;
    store(HIDE_DOTFILES_KEY, state.hideDotfiles ? "1" : "0");
    if (state.hideDotfiles) {
      // Don't leave now-invisible dot files selected for bulk actions.
      state.selection = new Set([...state.selection]
        .filter((path) => !path.split("/").pop().startsWith(".")));
      state.cursorIndex = null;
    }
    render();
  });

  $("btn-view-list").addEventListener("click", () => {
    state.view = "list";
    store(VIEW_KEY, "list");
    render();
  });

  $("btn-view-grid").addEventListener("click", () => {
    state.view = "grid";
    store(VIEW_KEY, "grid");
    render();
  });

  $("btn-logout").addEventListener("click", async () => {
    try {
      await api.logout();
    } catch {
      /* logging out locally is enough */
    }
    location.href = "/";
  });

  $("btn-drawer").addEventListener("click", () => {
    const shell = $("screen-browser");
    shell.dataset.drawer = shell.dataset.drawer === "open" ? "closed" : "open";
  });
  $("drawer-scrim").addEventListener("click", closeDrawer);
  bindSidebarResize();

  // Sidebar "Drive root" label accepts drops into the share root.
  const rootLabel = document.querySelector(".sidebar__label");
  if (rootLabel) bindFolderDrop(rootLabel, "");

  $("upload-close").addEventListener("click", () => {
    // Only dismiss finished/cancelled rows; leave active transfers running.
    const activeUp = state.uploads.some((e) => isTransferActive(e));
    const activeDown = state.downloads.some((e) => isTransferActive(e));
    if (activeUp || activeDown) {
      state.uploads = state.uploads.filter((e) => isTransferActive(e));
      state.downloads = state.downloads.filter((e) => isTransferActive(e));
    } else {
      state.uploads = [];
      state.downloads = [];
    }
    renderUploads();
  });
  $("upload-cancel").addEventListener("click", cancelUploads);

  // Empty / no-match / error state buttons
  document.addEventListener("click", (event) => {
    const action = event.target.closest("[data-act]");
    if (action) {
      const kind = action.dataset.act;
      if (kind === "upload") {
        openUploadMenu(action);
        return;
      }
      if (kind === "newfolder") openNewFolderModal();
      if (kind === "retry") loadRoute();
      if (kind === "unlock-personal") openPersonalUnlock(state.share, state.error?.status === 428);
      if (kind === "root") navigate("");
      if (kind === "clear-search") {
        clearSearch();
        render();
      }
      return;
    }
    // Any click outside an open row/upload menu dismisses it.
    if (document.getElementById("upload-menu") && !event.target.closest("#upload-menu, #btn-upload, [data-act=upload]")) {
      closeUploadMenu();
    }
    if (document.getElementById("sort-menu") && !event.target.closest("#sort-menu, #btn-sort-by")) {
      closeSortMenu();
    }
    if (document.getElementById("crumb-overflow-menu") && !event.target.closest("#crumb-overflow-menu, .crumb--more")) {
      closeCrumbOverflow();
    }
    if (state.openMenu && !event.target.closest(".menu, .row__dots")) {
      state.openMenu = null;
      state.menuAnchor = null;
      clearPortaledMenus();
      render();
    }
    if (
      state.shareMenuOpen &&
      !event.target.closest(".share-switch, .share-switch__menu")
    ) {
      closeShareMenu();
    }
    // Dismiss search typeahead when clicking outside the search control.
    if (searchUi.open && !event.target.closest("#search-wrap")) {
      closeSearchPanel();
    }
  });

  window.addEventListener("keydown", (event) => {
    if ($("screen-browser").hidden || !$("scrim").hidden) return;
    const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);

    if (event.key === "Escape") {
      if (searchUi.open) {
        closeSearchPanel();
        return;
      }
      if (document.getElementById("upload-menu")) {
        closeUploadMenu();
        return;
      }
      if (document.getElementById("sort-menu")) {
        closeSortMenu();
        return;
      }
      if (document.getElementById("crumb-overflow-menu")) {
        closeCrumbOverflow();
        return;
      }
      state.selection.clear();
      state.openMenu = null;
      state.menuAnchor = null;
      clearPortaledMenus();
      if (state.shareMenuOpen) closeShareMenu();
      if (typing) document.activeElement.blur();
      render();
      return;
    }
    if (typing) return;

    const mod = event.metaKey || event.ctrlKey;
    if ((event.key === "k" || event.key === "K" || event.key === "f" || event.key === "F") && mod && !event.shiftKey && !event.altKey) {
      event.preventDefault();
      $("search").focus();
      $("search").select();
      return;
    }
    if ((event.key === "n" || event.key === "N") && mod && event.shiftKey) {
      event.preventDefault();
      openNewFolderModal();
      return;
    }
    if ((event.key === "c" || event.key === "C") && mod && !event.shiftKey && !event.altKey && !hasTextSelection()) {
      event.preventDefault();
      void copyItemPaths(selectedItems());
      return;
    }
    if (event.key === "F2") {
      event.preventDefault();
      const [item] = selectedItems();
      const target = item || visibleItems()[state.cursorIndex];
      if (target) openRenameModal(target);
      return;
    }
    if (event.key === "?" && !mod) {
      event.preventDefault();
      openShortcutsModal();
      return;
    }

    // Keyboard navigation over the listing. The sidebar tree handles its own
    // arrow keys — don't double-drive the cursor while focus is in there.
    if (!event.target?.closest?.(".sidebar")) {
      const inGrid = state.view === "grid";
      const step = {
        ArrowDown: inGrid ? gridColumns() : 1,
        ArrowUp: inGrid ? -gridColumns() : -1,
        ArrowRight: inGrid ? 1 : 0,
        ArrowLeft: inGrid ? -1 : 0,
      }[event.key];
      if (step) {
        event.preventDefault();
        moveCursor(step, event.shiftKey);
        return;
      }
      // Enter/Space only when focus isn't on a control with its own activation
      // (buttons, links, tiles) — those already open/click on Enter.
      const onControl = Boolean(event.target?.closest?.("button, a, .tile"));
      if (event.key === "Enter" && !onControl && state.cursorIndex !== null) {
        const item = visibleItems()[state.cursorIndex];
        if (item) {
          event.preventDefault();
          openItem(item);
        }
        return;
      }
      if (event.key === " " && !onControl && state.cursorIndex !== null) {
        const item = visibleItems()[state.cursorIndex];
        if (item && previewKind(item)) {
          event.preventDefault();
          openPreview(item);
        }
        return;
      }
    }

    if ((event.key === "Backspace" || event.key === "Delete") && state.selection.size) {
      event.preventDefault();
      openDeleteModal(selectedItems());
      return;
    }
    if (event.key === "/") {
      event.preventDefault();
      $("search").focus();
      return;
    }
    if ((event.key === "a" || event.key === "A") && mod) {
      event.preventDefault();
      state.selection = new Set(visibleItems().map((item) => item.path));
      render();
      return;
    }
    if (
      !mod
      && !event.altKey
      && !state.openMenu
      && !document.getElementById("sort-menu")
      && !document.getElementById("upload-menu")
      && !document.getElementById("crumb-overflow-menu")
      && !event.target?.closest?.(".sidebar")
      && event.key.length === 1
      && /^[\p{L}\p{N}._#-]$/u.test(event.key)
    ) {
      event.preventDefault();
      jumpToTyped(event.key);
    }
  });

  // Drag-and-drop upload anywhere on the browser screen (OS files only).
  // In-app moves use IFD_DND and are handled by bindFolderDrop targets.
  window.addEventListener("dragenter", (event) => {
    if ($("screen-browser").hidden) return;
    if (isInternalDrag(event)) return;
    if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
    dragDepth += 1;
    $("dropzone-sub").textContent = `Uploading to ${folderLabel(state.path)}`;
    show($("dropzone"), true);
  });

  window.addEventListener("dragover", (event) => {
    if (!$("dropzone").hidden) event.preventDefault();
  });

  window.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) show($("dropzone"), false);
  });

  window.addEventListener("drop", (event) => {
    if ($("screen-browser").hidden) return;
    if (isInternalDrag(event)) {
      // Let folder drop targets handle it; don't treat as upload.
      dragDepth = 0;
      show($("dropzone"), false);
      return;
    }
    event.preventDefault();
    dragDepth = 0;
    show($("dropzone"), false);
    const files = [...(event.dataTransfer?.files || [])];
    if (files.length) startUploads(files);
  });

  window.addEventListener("hashchange", () => loadRoute());
}

/* ---------------------------------------------------------------------------
   Sidebar resize
   --------------------------------------------------------------------------- */
function clampSidebarWidth(px) {
  const max = Math.min(SIDEBAR_W_MAX, Math.floor(window.innerWidth * 0.45));
  return Math.min(max, Math.max(SIDEBAR_W_MIN, Math.round(px)));
}

function applySidebarWidth(px) {
  const w = clampSidebarWidth(px);
  const shell = $("screen-browser");
  if (shell) shell.style.setProperty("--sidebar-width", `${w}px`);
  return w;
}

function restoreSidebarWidth() {
  const raw = Number(readStored(SIDEBAR_W_KEY, String(SIDEBAR_W_DEFAULT)));
  applySidebarWidth(Number.isFinite(raw) ? raw : SIDEBAR_W_DEFAULT);
}

function bindSidebarResize() {
  const handle = $("sidebar-resize");
  const sidebar = $("sidebar");
  if (!handle || !sidebar) return;

  let dragging = false;
  let startX = 0;
  let startW = 0;

  const onMove = (event) => {
    if (!dragging) return;
    const clientX = event.touches ? event.touches[0].clientX : event.clientX;
    applySidebarWidth(startW + (clientX - startX));
  };

  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("is-dragging");
    document.body.classList.remove("is-resizing-sidebar");
    const current = Number.parseFloat(getComputedStyle($("screen-browser")).getPropertyValue("--sidebar-width"));
    if (Number.isFinite(current)) store(SIDEBAR_W_KEY, String(Math.round(current)));
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onUp);
  };

  handle.addEventListener("pointerdown", (event) => {
    if (window.matchMedia("(max-width: 900px)").matches) return;
    event.preventDefault();
    dragging = true;
    startX = event.clientX;
    startW = sidebar.getBoundingClientRect().width;
    handle.classList.add("is-dragging");
    document.body.classList.add("is-resizing-sidebar");
    handle.setPointerCapture?.(event.pointerId);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  });

  handle.addEventListener("dblclick", () => {
    applySidebarWidth(SIDEBAR_W_DEFAULT);
    store(SIDEBAR_W_KEY, String(SIDEBAR_W_DEFAULT));
  });

  handle.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 32 : 16;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      store(SIDEBAR_W_KEY, String(applySidebarWidth(sidebar.getBoundingClientRect().width - step)));
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      store(SIDEBAR_W_KEY, String(applySidebarWidth(sidebar.getBoundingClientRect().width + step)));
    } else if (event.key === "Home") {
      event.preventDefault();
      store(SIDEBAR_W_KEY, String(applySidebarWidth(SIDEBAR_W_MIN)));
    } else if (event.key === "End") {
      event.preventDefault();
      store(SIDEBAR_W_KEY, String(applySidebarWidth(SIDEBAR_W_MAX)));
    }
  });
}

/* ---------------------------------------------------------------------------
   LAN prefer — use the NAS LAN address when reachable (skip CF tunnel hop)
   --------------------------------------------------------------------------- */
function isLanHost(lanOrigin) {
  if (!lanOrigin) return false;
  try {
    const lanHost = new URL(lanOrigin).hostname.toLowerCase();
    return lanHost && location.hostname.toLowerCase() === lanHost;
  } catch {
    return false;
  }
}

/**
 * Detect whether the browser can reach the LAN gateway.
 *
 * Chrome 141+ blocks public-site → private-IP subresources (Local Network Access)
 * unless the request opts in with targetAddressSpace: "local". Image probes fail
 * with "Permission was denied for this request to access the local address space".
 * fetch(..., { mode: "no-cors", targetAddressSpace: "local" }) is the supported path.
 */
async function probeLanReachable(origin, timeoutMs = 600) {
  const base = origin.replace(/\/$/, "");
  const url = `${base}/api/health?lanprobe=${Date.now()}`;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    // Opaque success (status 0) means the TCP/HTTP exchange completed.
    // Network failures / LNA denial / timeout throw.
    await fetch(url, {
      mode: "no-cors",
      cache: "no-store",
      signal: ctrl.signal,
      // Chromium Local Network Access (ignored by browsers that don't implement it).
      targetAddressSpace: "local",
    });
    return true;
  } catch {
    // Don't chain an image probe — it always added ~800ms on failure (every
    // away-from-home boot) and Local Network Access blocks it the same way.
    return false;
  } finally {
    clearTimeout(timer);
  }
}

async function switchToLan(me) {
  const lanOrigin = (me?.lan_origin || "").replace(/\/$/, "");
  if (!lanOrigin) return false;
  try {
    localStorage.removeItem(FORCE_CLOUD_KEY);
  } catch {
    /* ignore */
  }
  const next = location.hash && location.hash !== "#" ? location.hash : "#/";
  if (me?.authenticated) {
    try {
      const handoff = await api.mintLanHandoff();
      const token = handoff?.token;
      const dest = (handoff?.lan_origin || lanOrigin).replace(/\/$/, "");
      if (token) {
        location.replace(
          `${dest}/auth/lan-handoff?token=${encodeURIComponent(token)}&next=${encodeURIComponent(next)}`,
        );
        return true;
      }
    } catch {
      /* fall through to bare LAN open */
    }
  }
  location.replace(`${lanOrigin}/${next.startsWith("#") ? next : ""}`);
  return true;
}

/**
 * If the public tunnel site can reach the LAN gateway, switch there.
 * Returns true when a navigation was started (caller should stop boot).
 */
async function maybePreferLan(me) {
  const params = new URLSearchParams(location.search);
  if (params.get("nolan") === "1" || params.get("cloud") === "1") {
    store(FORCE_CLOUD_KEY, "1");
    return false;
  }
  // Explicit LAN intent clears a prior "stay on cloud" choice.
  if (params.get("lan") === "1") {
    try {
      localStorage.removeItem(FORCE_CLOUD_KEY);
    } catch {
      /* ignore */
    }
  }
  if (readStored(FORCE_CLOUD_KEY, "") === "1" && params.get("lan") !== "1") return false;

  const lanOrigin = (me?.lan_origin || "").replace(/\/$/, "");
  if (!me?.lan_prefer || !lanOrigin) return false;
  if (isLanHost(lanOrigin) || me.via_lan) return false;

  const forceLan = params.get("lan") === "1";
  const reachable = forceLan || (await probeLanReachable(lanOrigin, 600));
  if (!reachable) return false;

  // Only hand off after sign-in. OAuth callback always lands on the public
  // host (Google redirect_uri); bouncing unsigned users to LAN first caused
  // "Invalid OAuth state" when the session cookie stayed on the LAN origin.
  if (!me.authenticated) {
    // ?lan=1 while signed out: open LAN login (separate cookie jar is fine).
    if (forceLan) {
      location.replace(`${lanOrigin}/${location.hash || ""}`);
      return true;
    }
    return false;
  }

  return switchToLan(me);
}

function updateLanBadge(me) {
  const badge = $("lan-badge");
  if (!badge) return;
  const lanOrigin = (me?.lan_origin || "").replace(/\/$/, "");
  const publicBase = (me?.public_base_url || location.origin).replace(/\/$/, "");

  // Already on LAN — show status + escape hatch to cloud.
  if (me?.via_lan) {
    badge.hidden = false;
    badge.innerHTML = "";
    badge.append(
      el("span", { class: "lan-badge__dot", "aria-hidden": "true" }),
      el("span", { text: "Local network" }),
      el(
        "button",
        {
          type: "button",
          class: "lan-badge__link",
          title: "Use Cloudflare tunnel instead",
          onclick: () => {
            store(FORCE_CLOUD_KEY, "1");
            location.href = `${publicBase}/?cloud=1${location.hash || ""}`;
          },
        },
        ["Remote"],
      ),
    );
    return;
  }

  // On public host with LAN configured: offer a manual switch (Chrome LNA can
  // block auto-probe until permission is granted; a click always works).
  if (me?.lan_prefer && lanOrigin && !isLanHost(lanOrigin)) {
    badge.hidden = false;
    badge.innerHTML = "";
    badge.append(
      el("span", { class: "lan-badge__dot lan-badge__dot--off", "aria-hidden": "true" }),
      el("span", { text: "Remote" }),
      el(
        "button",
        {
          type: "button",
          class: "lan-badge__link",
          title: "Switch to the LAN address (faster on the home network)",
          onclick: () => {
            switchToLan(me || { lan_origin: lanOrigin, authenticated: false });
          },
        },
        ["Use LAN"],
      ),
    );
    return;
  }

  badge.hidden = true;
  badge.textContent = "";
}

/* ---------------------------------------------------------------------------
   Boot
   --------------------------------------------------------------------------- */
async function boot() {
  if (location.pathname === "/open") {
    history.replaceState(null, "", `/${openLinkHash(location.search)}`);
  }
  // Prefer localStorage over whatever class survived the reload (avoids half-switched UI).
  applyTheme(storedTheme());
  restoreSidebarWidth();
  bindEvents();

  // Restore last share before any file API so listings hit the right root.
  // /api/me itself skips the share header and reads the server session.
  const storedShare = readStored(SHARE_KEY, "");
  if (storedShare) api.setActiveShare(storedShare);

  let me;
  try {
    me = await api.getMe();
  } catch {
    // Still try LAN from public config if /api/me failed hard.
    try {
      const cfg = await api.getConfig();
      if (await maybePreferLan({ ...cfg, authenticated: false })) return;
      state.me = { ...cfg, authenticated: false };
    } catch {
      /* stay on this host */
    }
    showLogin();
    return;
  }

  if (!me || !me.authenticated) {
    // Prefer the LAN path when the gateway is reachable.
    if (await maybePreferLan(me || { authenticated: false })) return;
    state.me = me || null;
    showLogin();
    return;
  }

  // Probe the LAN gateway alongside the first listing, not before it: off
  // the LAN the probe runs to its full timeout and used to delay every boot.
  const lanSwitch = maybePreferLan(me);
  state.me = me;
  setQuickScope(me.nas_username, state.share);
  showBrowser();
  updateLanBadge(me);
  // If localStorage preferred a different share than session, re-pin session.
  if (state.share && state.share !== me.share) {
    try {
      await api.setShare(state.share);
    } catch {
      /* keep session share if pin fails */
    }
  }
  loadUsage();
  refreshShortcuts();
  const listing = loadRoute();
  if (await lanSwitch) return;
  await listing;
}

boot();
