/**
 * Public shared file / folder page. No Drive chrome, no session.
 */
import { el, icon } from "./dom.js?v=20261027-mac";
import { describeKind, displayName, formatSize, previewKind } from "./format.js?v=20261027-mac";
import { enhanceMarkdownPreview, isMarkdown } from "./markdown.js?v=20261027-mac";
import { createMediaPlayer } from "./player.js?v=20261027-mac";

const THEME_KEY = "ifd-theme";
const TEXT_LIMIT_BYTES = 1_500_000;

const ERROR_COPY = {
  invalid: "This link isn’t valid.",
  expired: "This link expired.",
  unavailable: "This file or folder is no longer available.",
};

/** The one media player on screen, torn down when the view changes. */
let activePlayer = null;
/** Latest navigation; older fetches that land late are dropped. */
let showSeq = 0;

function tokenFromPath() {
  const raw = location.pathname.replace(/^\/s\//, "").replace(/\/+$/, "");
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

function applyTheme(theme) {
  const light = theme === "light";
  const root = document.documentElement;
  root.classList.toggle("light", light);
  root.dataset.theme = light ? "light" : "dark";
  root.style.colorScheme = light ? "light" : "dark";
  const scheme = document.getElementById("meta-color-scheme");
  const color = document.getElementById("meta-theme-color");
  if (scheme) scheme.setAttribute("content", light ? "light" : "dark");
  if (color) color.setAttribute("content", light ? "#f4f6f5" : "#0f110f");
  const use = document.getElementById("share-theme-icon");
  if (use) use.setAttribute("href", light ? "#i-moon" : "#i-sun");
  try {
    localStorage.setItem(THEME_KEY, light ? "light" : "dark");
  } catch {
    /* private mode */
  }
}

function storedTheme() {
  try {
    return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

function formatExpiry(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** `path` is the item inside a folder link ("" for the link itself). */
function fileUrls(token, path = "") {
  const base = `/api/s/${encodeURIComponent(token)}`;
  const q = path ? `?path=${encodeURIComponent(path)}` : "";
  return {
    meta: `${base}${q}`,
    inline: `${base}/file${q}${q ? "&" : "?"}inline=1`,
    download: `${base}/file${q}`,
    zip: `${base}/zip${q}`,
  };
}

/** Position inside a folder link lives in the #fragment, so it never reaches server logs. */
function currentPath() {
  try {
    return decodeURIComponent(location.hash.slice(1));
  } catch {
    return "";
  }
}

function pageUrl(path) {
  const hash = path ? `#${path.split("/").map(encodeURIComponent).join("/")}` : "";
  return `${location.pathname}${hash}`;
}

function mount(node) {
  const main = document.getElementById("share-main");
  if (!main) return;
  main.replaceChildren(node);
}

function errorCard(code, extra) {
  const title = ERROR_COPY[code] || ERROR_COPY.invalid;
  const bits = [el("h1", { class: "share__title", text: title })];
  if (code === "expired" && extra?.expires_at) {
    bits.push(el("p", { class: "share__meta", text: `Expired ${formatExpiry(extra.expires_at)}` }));
  } else {
    bits.push(
      el("p", {
        class: "share__meta",
        text: "Ask whoever sent this for a new link.",
      }),
    );
  }
  return el("div", { class: "share__card share__card--error" }, [
    el("div", { class: "share__kind", "aria-hidden": "true" }, [icon("#i-alert", 22)]),
    ...bits,
  ]);
}

function renderPreview(item, inlineUrl, downloadUrl) {
  const kind = previewKind(item) || item.kind;
  const stage = el("div", { class: `share__preview share__preview--${kind || "file"}` });

  if (kind === "image") {
    const img = el("img", {
      class: "share__img",
      src: inlineUrl,
      alt: displayName(item.name),
    });
    img.addEventListener("error", () => {
      stage.replaceChildren(el("p", { class: "share__preview-fail", text: "Preview couldn’t load. Download the file instead." }));
    });
    stage.append(img);
    return stage;
  }

  if (kind === "video") {
    const player = createMediaPlayer("video", inlineUrl, {
      fullscreenTarget: stage,
      sizeBytes: item.size,
      onDownload: () => {
        window.location.href = downloadUrl;
      },
      onError: () => {
        stage.replaceChildren(
          el("p", {
            class: "share__preview-fail",
            text: "This video format can’t play in the browser. Download it instead.",
          }),
        );
      },
    });
    stage.append(player.node);
    activePlayer = player;
    void player.start();
    return stage;
  }

  if (kind === "audio") {
    const player = createMediaPlayer("audio", inlineUrl, {
      onError: () => {
        stage.replaceChildren(
          el("p", {
            class: "share__preview-fail",
            text: "This audio format can’t play in the browser. Download it instead.",
          }),
        );
      },
    });
    stage.append(
      el("div", { class: "aplayer" }, [
        el("div", { class: "aplayer__art" }, [icon("#i-audio", 34)]),
        el("div", { class: "aplayer__name", text: displayName(item.name) }),
        el("div", { class: "aplayer__meta", text: `${describeKind(item).label} · ${formatSize(item.size)}` }),
        player.node,
      ]),
    );
    activePlayer = player;
    void player.start();
    return stage;
  }

  if (kind === "pdf") {
    stage.append(
      el("iframe", {
        class: "share__pdf",
        src: `${inlineUrl}#toolbar=0&navpanes=0&view=FitH`,
        title: displayName(item.name),
      }),
    );
    return stage;
  }

  if (kind === "text") {
    stage.append(el("div", { class: "share__preview-fail", text: "Loading…" }));
    void (async () => {
      try {
        const res = await fetch(inlineUrl, { credentials: "same-origin", cache: "no-store" });
        if (!res.ok) throw new Error("load failed");
        const blob = await res.blob();
        const slice = blob.size > TEXT_LIMIT_BYTES ? blob.slice(0, TEXT_LIMIT_BYTES) : blob;
        let text = await slice.text();
        if (blob.size > TEXT_LIMIT_BYTES) {
          text += `\n\n… truncated (${formatSize(blob.size)} file; showing first 1.5 MB)`;
        }
        const pre = el("pre", { class: "share__text", text });
        stage.replaceChildren(pre);
        if (isMarkdown(item.name)) {
          stage.classList.add("share__preview--markdown");
          const bar = el("div", { class: "code__bar" });
          stage.append(bar);
          enhanceMarkdownPreview({ container: stage, raw: pre, bar, text });
        }
      } catch {
        stage.replaceChildren(
          el("p", { class: "share__preview-fail", text: "Couldn’t preview this file. Download it instead." }),
        );
      }
    })();
    return stage;
  }

  return null;
}

/** Folder › sub › item trail inside a folder link; null at the link's top level. */
function crumbs(data, go) {
  const parts = (data.path || "").split("/").filter(Boolean);
  if (!parts.length) return null;
  const trail = [{ label: data.folder || "Shared folder", path: "" }];
  parts.forEach((part, i) => trail.push({ label: part, path: parts.slice(0, i + 1).join("/") }));
  const nodes = [];
  trail.forEach((crumb, i) => {
    if (i) nodes.push(el("span", { class: "share__crumb-sep", "aria-hidden": "true" }, [icon("#i-chev-r", 12)]));
    nodes.push(
      i === trail.length - 1
        ? el("span", { class: "share__crumb", "aria-current": "page", text: displayName(crumb.label) })
        : el("a", {
            class: "share__crumb",
            href: pageUrl(crumb.path),
            text: displayName(crumb.label),
            onclick: (event) => go(event, crumb.path),
          }),
    );
  });
  return el("nav", { class: "share__crumbs", "aria-label": "Folder" }, nodes);
}

function folderCard(data, token, go) {
  const expiry = formatExpiry(data.expires_at);
  const count = data.items.length;
  const rows = data.items.map((item) => {
    const kindMeta = describeKind(item);
    return el("li", { class: "share__item" }, [
      el(
        "a",
        { class: "share__row", href: pageUrl(item.path), onclick: (event) => go(event, item.path) },
        [
          el("span", { class: "share__row-icon", style: `color:${kindMeta.color}` }, [icon(kindMeta.icon, 18)]),
          el("span", { class: "share__row-name", text: displayName(item.name) }),
          el("span", { class: "share__row-meta", text: item.is_dir ? "" : formatSize(item.size) }),
        ],
      ),
      item.is_dir
        ? null
        : el(
            "a",
            {
              class: "icon-btn share__row-dl",
              href: fileUrls(token, item.path).download,
              "aria-label": `Download ${displayName(item.name)}`,
              title: "Download",
            },
            [icon("#i-download", 15)],
          ),
    ]);
  });

  return el("div", { class: "share__card" }, [
    crumbs(data, go),
    el("div", { class: "share__head" }, [
      el("div", { class: "share__kind", style: `color:${describeKind({ is_dir: true, name: data.name }).color}` }, [
        icon("#i-folder", 22),
      ]),
      el("div", { class: "share__head-text" }, [
        el("h1", { class: "share__title", text: displayName(data.name) }),
        el("p", {
          class: "share__meta",
          text: [`${count} item${count === 1 ? "" : "s"}`, expiry ? `Expires ${expiry}` : ""].filter(Boolean).join(" · "),
        }),
      ]),
    ]),
    count
      ? el("ul", { class: "share__list" }, rows)
      : el("p", { class: "share__preview-fail", text: "This folder is empty." }),
    count
      ? el("a", { class: "btn btn--primary btn--modal share__download", href: fileUrls(token, data.path).zip }, [
          icon("#i-download", 16),
          "Download folder",
        ])
      : null,
  ]);
}

function fileCard(data, urls, go) {
  const item = { name: data.name, size: data.size, is_dir: false };
  const kindMeta = describeKind(item);
  const expiry = formatExpiry(data.expires_at);
  const preview = data.previewable ? renderPreview({ ...item, kind: data.kind }, urls.inline, urls.download) : null;

  return el("div", { class: "share__card" }, [
    crumbs(data, go),
    el("div", { class: "share__head" }, [
      el("div", { class: "share__kind", style: `color:${kindMeta.color}` }, [icon(kindMeta.icon, 22)]),
      el("div", { class: "share__head-text" }, [
        el("h1", { class: "share__title", text: displayName(data.name) }),
        el("p", {
          class: "share__meta",
          text: [kindMeta.label, formatSize(data.size), expiry ? `Expires ${expiry}` : ""]
            .filter(Boolean)
            .join(" · "),
        }),
      ]),
    ]),
    preview,
    el(
      "a",
      { class: "btn btn--primary btn--modal share__download", href: urls.download },
      [icon("#i-download", 16), "Download"],
    ),
  ]);
}

async function boot() {
  applyTheme(storedTheme());
  const themeBtn = document.getElementById("share-theme");
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      applyTheme(storedTheme() === "light" ? "dark" : "light");
    });
  }

  const token = tokenFromPath();
  if (!token) {
    mount(errorCard("invalid"));
    return;
  }
  // Inside a folder link, rows and crumbs navigate in place (#sub/path) so Back works.
  const go = (event, path) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    history.pushState(null, "", pageUrl(path));
    window.scrollTo(0, 0);
    void show(token, path, go);
  };
  window.addEventListener("popstate", () => void show(token, currentPath(), go));
  await show(token, currentPath(), go);
}

async function show(token, path, go) {
  const seq = ++showSeq;
  const urls = fileUrls(token, path);
  try {
    const res = await fetch(urls.meta, { credentials: "same-origin", cache: "no-store" });
    let data = {};
    try {
      data = await res.json();
    } catch {
      data = {};
    }
    if (seq !== showSeq) return;
    activePlayer?.destroy();
    activePlayer = null;
    if (!res.ok) {
      document.title = `Shared · ${siteName()}`;
      mount(errorCard(data.error || "invalid", data));
      return;
    }
    document.title = `${displayName(data.name)} · ${siteName()}`;
    mount(data.is_dir ? folderCard(data, token, go) : fileCard(data, urls, go));
  } catch {
    if (seq === showSeq) mount(errorCard("unavailable"));
  }
}

boot();

/** This install's name, filled into the page by the server. */
function siteName() {
  return document.querySelector("[data-drive-name]")?.textContent?.trim() || "Drive";
}
