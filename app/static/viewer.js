/**
 * Media / document preview lightbox.
 *
 * Owns the shell (header, sibling navigation, footer hints) and one renderer
 * per preview kind. The host app injects modal plumbing and download actions
 * so this module stays free of app.js internals.
 */
import { el, icon } from "./dom.js?v=20261027-mac";
import { describeKind, displayName, formatSize, previewKind } from "./format.js?v=20261027-mac";
import { createMediaPlayer } from "./player.js?v=20261027-mac";
import { enhanceMarkdownPreview, isMarkdown } from "./markdown.js?v=20261027-mac";

const TEXT_LIMIT_BYTES = 1_500_000;
const ZOOM_MIN = 1;
const ZOOM_MAX = 8;
const ZOOM_STEP = 1.35;

/**
 * @param {object} item  file to open
 * @param {{
 *   siblings: object[],
 *   downloadUrl: (path: string, opts: object) => string,
 *   download: (items: object[]) => void,
 *   openModal: (node: HTMLElement, opts: object) => void,
 *   closeModal: () => void,
 *   onScrimReady?: () => void,
 * }} host
 */
export function openPreview(item, host) {
  const siblings = host.siblings.length ? host.siblings.slice() : [item];
  let index = siblings.findIndex((entry) => entry.path === item.path);
  if (index < 0) {
    siblings.unshift(item);
    index = 0;
  }

  /** Teardown for whatever the stage currently holds (player timers, etc). */
  let disposeStage = null;
  /** Live player for the current item, so keyboard shortcuts can reach it. */
  let player = null;

  const stage = el("div", { class: "viewer__stage" });
  const title = el("div", { class: "viewer__title", text: displayName(item.name) });
  const meta = el("div", { class: "viewer__meta", text: "" });
  const counter = el("div", { class: "viewer__counter", text: "" });
  const hint = el("div", { class: "viewer__hint" });

  const navButton = (dir) =>
    el(
      "button",
      {
        type: "button",
        class: `viewer__nav viewer__nav--${dir}`,
        "aria-label": dir === "prev" ? "Previous" : "Next",
        onclick: () => showAt(index + (dir === "prev" ? -1 : 1)),
      },
      [icon("#i-chev-r", 20)],
    );
  const prevBtn = navButton("prev");
  const nextBtn = navButton("next");

  const downloadBtn = el(
    "button",
    {
      type: "button",
      class: "btn btn--modal",
      onclick: () => {
        const current = siblings[index];
        if (current) host.download([current]);
      },
    },
    [icon("#i-download", 15), "Download"],
  );

  const closeBtn = el(
    "button",
    { type: "button", class: "viewer__close", "aria-label": "Close preview", onclick: () => host.closeModal() },
    [icon("#i-x", 18)],
  );

  const body = el("div", { class: "viewer__body" }, [stage]);
  const viewer = el("div", { class: "viewer", onclick: (event) => event.stopPropagation() }, [
    el("div", { class: "viewer__top" }, [
      el("div", { class: "viewer__heading" }, [title, meta]),
      el("div", { class: "viewer__actions" }, [counter, downloadBtn, closeBtn]),
    ]),
    body,
    hint,
  ]);

  /* -------------------------------------------------------------------------
     Stage renderers
     ------------------------------------------------------------------------- */

  function fallback(current, message) {
    return el("div", { class: "viewer__fallback" }, [
      el("div", { class: "viewer__fallback-icon" }, [icon(describeKind(current).icon, 26)]),
      el("div", { class: "viewer__fallback-title", text: displayName(current.name) }),
      el("p", { text: message }),
      el(
        "button",
        { type: "button", class: "btn btn--primary", onclick: () => host.download([current]) },
        [icon("#i-download", 15), "Download"],
      ),
    ]);
  }

  function failStage(current, message) {
    stage.replaceChildren(fallback(current, message));
    mountNav();
  }

  /** Pan/zoom image surface: wheel to zoom at cursor, drag to pan, dbl-click to toggle. */
  function renderImage(current, url) {
    const img = el("img", { class: "izoom__img", src: url, alt: current.name, draggable: "false" });
    const surface = el("div", { class: "izoom" }, [img]);
    const level = el("span", { class: "izoom__level", text: "100%" });

    let scale = 1;
    let x = 0;
    let y = 0;

    function apply() {
      img.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
      surface.classList.toggle("is-zoomed", scale > 1);
      level.textContent = `${Math.round(scale * 100)}%`;
    }

    /** Zoom about a viewport point so the pixel under the cursor stays put. */
    function zoomTo(next, originX, originY) {
      const clamped = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next));
      const box = surface.getBoundingClientRect();
      const cx = (originX ?? box.left + box.width / 2) - box.left - box.width / 2;
      const cy = (originY ?? box.top + box.height / 2) - box.top - box.height / 2;
      const ratio = clamped / scale;
      x = cx - (cx - x) * ratio;
      y = cy - (cy - y) * ratio;
      scale = clamped;
      if (scale === 1) {
        x = 0;
        y = 0;
      }
      apply();
    }

    surface.addEventListener("wheel", (event) => {
      event.preventDefault();
      zoomTo(scale * (event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP), event.clientX, event.clientY);
    }, { passive: false });

    surface.addEventListener("dblclick", (event) => {
      if (scale > 1) zoomTo(1);
      else zoomTo(2.5, event.clientX, event.clientY);
    });

    let panning = null;
    surface.addEventListener("pointerdown", (event) => {
      if (scale <= 1 || event.button !== 0) return;
      event.preventDefault();
      panning = { startX: event.clientX - x, startY: event.clientY - y, id: event.pointerId };
      surface.setPointerCapture(event.pointerId);
      surface.classList.add("is-panning");
    });
    surface.addEventListener("pointermove", (event) => {
      if (!panning) return;
      x = event.clientX - panning.startX;
      y = event.clientY - panning.startY;
      apply();
    });
    const endPan = () => {
      if (!panning) return;
      try {
        surface.releasePointerCapture(panning.id);
      } catch {
        /* pointer already released */
      }
      panning = null;
      surface.classList.remove("is-panning");
    };
    surface.addEventListener("pointerup", endPan);
    surface.addEventListener("pointercancel", endPan);

    const zoomBar = el("div", { class: "izoom__bar" }, [
      el("button", { type: "button", class: "vctl", "aria-label": "Zoom out", title: "Zoom out", onclick: () => zoomTo(scale / ZOOM_STEP) }, [icon("#i-zoom-out", 16)]),
      level,
      el("button", { type: "button", class: "vctl", "aria-label": "Zoom in", title: "Zoom in", onclick: () => zoomTo(scale * ZOOM_STEP) }, [icon("#i-zoom-in", 16)]),
      el("button", { type: "button", class: "vctl", "aria-label": "Fit to screen", title: "Fit to screen", onclick: () => zoomTo(1) }, [icon("#i-fit", 16)]),
    ]);

    img.addEventListener("load", () => {
      stage.classList.remove("is-loading");
      if (img.naturalWidth) {
        meta.textContent = `${describeKind(current).label} · ${img.naturalWidth}×${img.naturalHeight} · ${formatSize(current.size)}`;
      }
    });
    img.addEventListener("error", () => {
      stage.classList.remove("is-loading");
      failStage(current, "This image can’t be displayed in the browser.");
    });

    stage.classList.add("is-loading");
    stage.replaceChildren(surface, zoomBar);
    mountNav();
  }

  function renderVideo(current, url) {
    player = createMediaPlayer("video", url, {
      fullscreenTarget: viewer,
      sizeBytes: current.size,
      onDownload: () => host.download([current]),
      onError: () => failStage(current, "This video format can’t play in the browser. Try downloading it."),
    });
    disposeStage = player.destroy;
    stage.replaceChildren(player.node);
    mountNav();
    void player.start();
  }

  function renderAudio(current, url) {
    player = createMediaPlayer("audio", url, {
      onError: () => failStage(current, "This audio format can’t play in the browser. Try downloading it."),
    });
    disposeStage = player.destroy;
    const card = el("div", { class: "aplayer" }, [
      el("div", { class: "aplayer__art" }, [icon("#i-audio", 34)]),
      el("div", { class: "aplayer__name", text: displayName(current.name) }),
      el("div", { class: "aplayer__meta", text: `${describeKind(current).label} · ${formatSize(current.size)}` }),
      player.node,
    ]);
    stage.replaceChildren(card);
    mountNav();
    void player.start();
  }

  /**
   * Render PDF pages on our own paper surface (no Chrome grey toolbar /
   * thumbnail rail). Falls back to a plain iframe if pdf.js can't load.
   */
  function renderPdf(current, url) {
    stage.classList.add("is-loading");

    const label = el("span", { class: "docview__label", text: "PDF · loading…" });
    const pages = el("div", { class: "docview__pages" });
    const bar = el("div", { class: "docview__bar" }, [
      label,
      el("div", { class: "docview__bar-actions" }, [
        el(
          "a",
          {
            class: "vctl vctl--wide",
            href: url,
            target: "_blank",
            rel: "noopener",
            title: "Open in the browser’s full PDF viewer",
          },
          [icon("#i-ext", 15), "Open in new tab"],
        ),
      ]),
    ]);
    const shell = el("div", { class: "docview" }, [pages, bar]);
    stage.replaceChildren(shell);
    mountNav();

    let cancelled = false;
    disposeStage = () => {
      cancelled = true;
    };

    void (async () => {
      try {
        const res = await fetch(url, { credentials: "same-origin" });
        if (!res.ok) throw new Error(res.statusText || "Failed to load");
        const data = new Uint8Array(await res.arrayBuffer());
        if (cancelled || siblings[index]?.path !== current.path) return;

        const PDFJS_VER = "4.10.38";
        const pdfjs = await import(
          /* @vite-ignore */ `https://cdn.jsdelivr.net/npm/pdfjs-dist@${PDFJS_VER}/build/pdf.min.mjs`
        );
        pdfjs.GlobalWorkerOptions.workerSrc =
          `https://cdn.jsdelivr.net/npm/pdfjs-dist@${PDFJS_VER}/build/pdf.worker.min.mjs`;

        const doc = await pdfjs.getDocument({ data }).promise;
        if (cancelled || siblings[index]?.path !== current.path) {
          doc.destroy().catch(() => {});
          return;
        }

        disposeStage = () => {
          cancelled = true;
          doc.destroy().catch(() => {});
        };

        const total = doc.numPages;
        label.textContent = total === 1 ? "PDF · 1 page" : `PDF · ${total} pages · scroll to browse`;

        // Wait a frame so the stage has real clientWidth/Height before scaling.
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        if (cancelled || siblings[index]?.path !== current.path) return;

        const padX = 40;
        const padY = 48;
        const availW = Math.max(240, Math.min(820, (pages.clientWidth || 720) - padX));
        // Prefer fitting the stage height so a one-page resume is fully visible.
        const availH = Math.max(
          240,
          (pages.clientHeight || Math.floor(window.innerHeight * 0.7)) - padY,
        );

        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        for (let n = 1; n <= total; n += 1) {
          if (cancelled) return;
          const page = await doc.getPage(n);
          if (cancelled) return;

          const base = page.getViewport({ scale: 1 });
          const scaleW = availW / base.width;
          // Single-page: fit entire page in the stage (width AND height).
          // Multi-page: fit width; user scrolls for the rest.
          const scaleH = availH / base.height;
          const cssScale =
            total === 1 ? Math.min(scaleW, scaleH, 2.25) : Math.min(scaleW, 2.25);
          const viewport = page.getViewport({ scale: cssScale * dpr });
          const canvas = el("canvas", { class: "docview__sheet" });
          canvas.width = Math.floor(viewport.width);
          canvas.height = Math.floor(viewport.height);
          canvas.style.width = `${Math.floor(viewport.width / dpr)}px`;
          canvas.style.height = "auto";
          canvas.style.maxWidth = "100%";

          const ctx = canvas.getContext("2d", { alpha: false });
          await page.render({ canvasContext: ctx, viewport }).promise;
          if (cancelled) return;
          pages.append(canvas);
          if (n === 1) stage.classList.remove("is-loading");
        }
        // Jump scroll to top so a tall page never opens mid-clipped.
        pages.scrollTop = 0;
        stage.classList.remove("is-loading");
      } catch (error) {
        if (cancelled || siblings[index]?.path !== current.path) return;
        // pdf.js CDN blocked / parse error → plain iframe (browser chrome).
        console.warn("pdf.js render failed, falling back to iframe", error);
        label.textContent = "PDF · scroll to page";
        const frame = el("iframe", {
          class: "docview__frame",
          src: `${url}#toolbar=0&navpanes=0&statusbar=0&view=FitH`,
          title: current.name,
        });
        frame.addEventListener("load", () => stage.classList.remove("is-loading"));
        pages.replaceChildren(frame);
        pages.classList.add("docview__pages--frame");
        stage.classList.remove("is-loading");
      }
    })();
  }

  async function renderText(current, url) {
    stage.classList.add("is-loading");
    stage.replaceChildren(el("div", { class: "viewer__loading", text: "Loading…" }));
    mountNav();

    let text;
    try {
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) throw new Error(res.statusText || "Failed to load");
      const blob = await res.blob();
      const slice = blob.size > TEXT_LIMIT_BYTES ? blob.slice(0, TEXT_LIMIT_BYTES) : blob;
      text = await slice.text();
      if (blob.size > TEXT_LIMIT_BYTES) {
        text += `\n\n… truncated (${formatSize(blob.size)} file; showing first 1.5 MB)`;
      }
    } catch (error) {
      if (siblings[index]?.path !== current.path) return;
      stage.classList.remove("is-loading");
      failStage(current, error.message || "Couldn’t load this file.");
      return;
    }

    if (siblings[index]?.path !== current.path) return; // navigated away mid-fetch
    stage.classList.remove("is-loading");

    const lines = text.split("\n");
    const gutter = el("div", { class: "code__gutter", "aria-hidden": "true" });
    gutter.textContent = lines.map((_, i) => i + 1).join("\n");
    const code = el("pre", { class: "code__text", text });
    const scroll = el("div", { class: "code__scroll" }, [gutter, code]);
    // Unwrapped by default so the line-number gutter stays aligned; the
    // toggle is there for prose and long log lines.
    const wrapper = el("div", { class: "code" }, [scroll]);

    const wrapBtn = el(
      "button",
      { type: "button", class: "vctl vctl--wide", title: "Toggle soft wrap" },
      [icon("#i-wrap", 15), "Wrap"],
    );
    wrapBtn.addEventListener("click", () => {
      wrapBtn.classList.toggle("is-on", wrapper.classList.toggle("is-wrapped"));
    });

    const copyBtn = el(
      "button",
      { type: "button", class: "vctl vctl--wide", title: "Copy contents" },
      [icon("#i-copy", 15), "Copy"],
    );
    copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(text);
        copyBtn.replaceChildren(icon("#i-check", 15), document.createTextNode("Copied"));
        setTimeout(() => copyBtn.replaceChildren(icon("#i-copy", 15), document.createTextNode("Copy")), 1400);
      } catch {
        copyBtn.replaceChildren(icon("#i-alert", 15), document.createTextNode("Blocked"));
        setTimeout(() => copyBtn.replaceChildren(icon("#i-copy", 15), document.createTextNode("Copy")), 1400);
      }
    });

    const bar = el("div", { class: "code__bar" }, [wrapBtn, copyBtn]);
    wrapper.append(bar);
    if (isMarkdown(current.name)) {
      enhanceMarkdownPreview({ container: wrapper, raw: scroll, bar, text, wrapButton: wrapBtn });
    }
    stage.replaceChildren(wrapper);
    mountNav();
  }

  /* -------------------------------------------------------------------------
     Shell
     ------------------------------------------------------------------------- */

  function mountNav() {
    stage.append(prevBtn, nextBtn);
    const solo = siblings.length < 2;
    for (const btn of [prevBtn, nextBtn]) {
      btn.disabled = solo;
      btn.hidden = solo;
    }
  }

  const HINTS = {
    video: "Space play/pause · ← → seek · ⇧← ⇧→ browse · ↑ ↓ volume · F fullscreen · Esc close",
    audio: "Space play/pause · ← → seek · ⇧← ⇧→ browse · ↑ ↓ volume · M mute · Esc close",
    image: "Scroll to zoom · drag to pan · double-click to fit · ← → to browse · Esc close",
  };

  function setMode(mode) {
    viewer.classList.toggle("viewer--video", mode === "video");
    viewer.classList.toggle("viewer--audio", mode === "audio");
    viewer.classList.toggle("viewer--image", mode === "image");
    viewer.classList.toggle("viewer--doc", mode === "pdf" || mode === "text");
    viewer.dataset.mode = mode || "file";
    hint.textContent = HINTS[mode] || "← → to browse · Esc to close";
  }

  function renderCurrent(current) {
    if (disposeStage) {
      disposeStage();
      disposeStage = null;
    }
    player = null;
    stage.classList.remove("is-loading");
    stage.replaceChildren();

    const kind = previewKind(current);
    const url = host.downloadUrl(current.path, { inline: true });
    title.textContent = displayName(current.name);
    meta.textContent = `${describeKind(current).label} · ${formatSize(current.size)}`;
    counter.textContent = siblings.length > 1 ? `${index + 1} / ${siblings.length}` : "";
    setMode(kind);

    if (kind === "image") renderImage(current, url);
    else if (kind === "video") renderVideo(current, url);
    else if (kind === "audio") renderAudio(current, url);
    else if (kind === "pdf") renderPdf(current, url);
    else if (kind === "text") void renderText(current, url);
    else failStage(current, "This file type can’t be previewed in the browser.");
  }

  function showAt(nextIndex) {
    if (!siblings.length) return;
    index = ((nextIndex % siblings.length) + siblings.length) % siblings.length;
    renderCurrent(siblings[index]);
  }

  function onKey(event) {
    if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
    const media = player?.media;
    const seekable = media && Number.isFinite(media.duration);

    switch (event.key) {
      // Arrows seek inside playable media; Shift+Arrow always walks siblings.
      case "ArrowLeft":
        event.preventDefault();
        if (seekable && !event.shiftKey) player.skip(-10);
        else showAt(index - 1);
        break;
      case "ArrowRight":
        event.preventDefault();
        if (seekable && !event.shiftKey) player.skip(10);
        else showAt(index + 1);
        break;
      case "ArrowUp":
      case "ArrowDown":
        if (!media) return;
        event.preventDefault();
        media.muted = false;
        media.volume = Math.min(1, Math.max(0, media.volume + (event.key === "ArrowUp" ? 0.1 : -0.1)));
        break;
      case " ":
        if (!media) return;
        event.preventDefault();
        player.togglePlay();
        break;
      case "m":
      case "M":
        if (!media) return;
        event.preventDefault();
        player.toggleMute();
        break;
      case "f":
      case "F":
        if (!player || viewer.dataset.mode !== "video") return;
        event.preventDefault();
        player.toggleFullscreen();
        break;
      default:
        break;
    }
  }

  host.openModal(viewer, { onKey, onClose: () => disposeStage?.() });
  if (host.onScrimReady) host.onScrimReady();
  renderCurrent(siblings[index]);
}
