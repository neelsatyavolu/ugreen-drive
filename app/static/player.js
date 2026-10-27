/**
 * Brand-native media transport for the preview lightbox.
 *
 * Replaces the browser's default <video>/<audio> chrome with controls that
 * match the Machine & Ink system: accent scrub track, buffered range, hover
 * scrub tooltip, tabular time readout, speed / PiP / fullscreen.
 */
import { el, icon } from "./dom.js?v=20261026-ugd2";

const VOLUME_KEY = "ifd-volume";
const MUTED_KEY = "ifd-muted";
const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2];
const IDLE_HIDE_MS = 2400;
const SKIP_SECONDS = 10;
/** Seconds to buffer ahead before starting, so playback doesn't stall instantly. */
const PREROLL_SECONDS = 6;
/** Give up waiting for pre-roll and just start; better than an indefinite spinner. */
const PREROLL_TIMEOUT_MS = 12000;
/** Sampling window for the throughput estimate that drives the stall warning. */
const THROUGHPUT_SAMPLE_MS = 5000;

/** "1:04" under an hour, "1:02:03" past it. Blank while duration is unknown. */
export function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const hrs = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return hrs > 0 ? `${hrs}:${pad(mins)}:${pad(secs)}` : `${mins}:${pad(secs)}`;
}

/** Bits/sec as "4.2 Mbps" — for the "your link can't keep up" notice. */
function formatBits(bps) {
  if (bps >= 1_000_000) return `${(bps / 1_000_000).toFixed(1)} Mbps`;
  return `${Math.round(bps / 1000)} kbps`;
}

function readVolume() {
  const stored = Number.parseFloat(localStorage.getItem(VOLUME_KEY) ?? "");
  return Number.isFinite(stored) ? Math.min(1, Math.max(0, stored)) : 1;
}

function readMuted() {
  return localStorage.getItem(MUTED_KEY) === "1";
}

function persist(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode — preference just won't persist */
  }
}

function ctrlButton(symbol, label, onClick, { size = 17, cls = "" } = {}) {
  return el(
    "button",
    { type: "button", class: `vctl ${cls}`.trim(), "aria-label": label, title: label, onclick: onClick },
    [icon(symbol, size)],
  );
}

/**
 * A draggable 0..1 track. Emits `onSeek(fraction)` live while dragging and
 * `onCommit(fraction)` on release so callers can distinguish preview vs apply.
 */
function createTrack({ cls, label, onScrub, onCommit, onHover }) {
  const buffered = el("div", { class: "vtrack__buffered" });
  const played = el("div", { class: "vtrack__played" });
  const handle = el("div", { class: "vtrack__handle" });
  const rail = el("div", { class: "vtrack__rail" }, [buffered, played, handle]);
  const node = el("div", {
    class: `vtrack ${cls}`.trim(),
    role: "slider",
    tabindex: "0",
    "aria-label": label,
    "aria-valuemin": "0",
    "aria-valuemax": "100",
    "aria-valuenow": "0",
  }, [rail]);

  let dragging = false;

  const fractionAt = (clientX) => {
    const box = rail.getBoundingClientRect();
    if (!box.width) return 0;
    return Math.min(1, Math.max(0, (clientX - box.left) / box.width));
  };

  node.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    dragging = true;
    node.classList.add("is-dragging");
    node.setPointerCapture(event.pointerId);
    onScrub(fractionAt(event.clientX));
  });

  node.addEventListener("pointermove", (event) => {
    if (dragging) {
      onScrub(fractionAt(event.clientX));
      return;
    }
    if (onHover) onHover(fractionAt(event.clientX), event.clientX);
  });

  node.addEventListener("pointerleave", () => {
    if (!dragging && onHover) onHover(null, 0);
  });

  const endDrag = (event) => {
    if (!dragging) return;
    dragging = false;
    node.classList.remove("is-dragging");
    try {
      node.releasePointerCapture(event.pointerId);
    } catch {
      /* pointer already gone */
    }
    if (onCommit) onCommit(fractionAt(event.clientX));
  };
  node.addEventListener("pointerup", endDrag);
  node.addEventListener("pointercancel", endDrag);

  return {
    node,
    isDragging: () => dragging,
    set(fraction, bufferedFraction = null) {
      const pct = `${Math.min(1, Math.max(0, fraction)) * 100}%`;
      played.style.width = pct;
      handle.style.left = pct;
      node.setAttribute("aria-valuenow", String(Math.round(fraction * 100)));
      if (bufferedFraction != null) {
        buffered.style.width = `${Math.min(1, Math.max(0, bufferedFraction)) * 100}%`;
      }
    },
  };
}

/**
 * Build a fully-custom player.
 *
 * @param {"video"|"audio"} kind
 * @param {string} src
 * @param {{ fullscreenTarget?: HTMLElement, onError?: () => void,
 *           sizeBytes?: number, onDownload?: () => void }} opts
 * @returns {{ node: HTMLElement, media: HTMLMediaElement, destroy: () => void,
 *             start: () => Promise<void>, togglePlay: () => void, skip: (n: number) => void,
 *             toggleFullscreen: () => void, toggleMute: () => void }}
 */
export function createMediaPlayer(kind, src, { fullscreenTarget, onError, sizeBytes, onDownload } = {}) {
  const isVideo = kind === "video";
  const media = el(isVideo ? "video" : "audio", {
    class: isVideo ? "vplayer__video" : "vplayer__audio",
    src,
    preload: isVideo ? "auto" : "metadata",
    playsinline: "",
  });
  media.volume = readVolume();
  media.muted = readMuted();

  /** Every interval/timeout this player owns, cleared on destroy. */
  const timers = [];

  /* ---- transport widgets ---- */
  const playBtn = ctrlButton("#i-play", "Play", () => togglePlay(), { size: 19, cls: "vctl--play" });
  const backBtn = ctrlButton("#i-back-10", `Back ${SKIP_SECONDS} seconds`, () => skip(-SKIP_SECONDS));
  const fwdBtn = ctrlButton("#i-fwd-10", `Forward ${SKIP_SECONDS} seconds`, () => skip(SKIP_SECONDS));
  const muteBtn = ctrlButton("#i-vol", "Mute", () => toggleMute());

  const timeNow = el("span", { class: "vtime__now", text: "0:00" });
  const timeTotal = el("span", { class: "vtime__total", text: "0:00" });
  const timeLabel = el("div", { class: "vtime" }, [timeNow, el("span", { class: "vtime__sep", text: "/" }), timeTotal]);

  const scrubTip = el("div", { class: "vscrub__tip", hidden: true });
  const scrub = createTrack({
    cls: "vtrack--scrub",
    label: "Seek",
    onScrub: (fraction) => {
      if (!Number.isFinite(media.duration)) return;
      scrub.set(fraction);
      timeNow.textContent = formatTime(fraction * media.duration);
    },
    onCommit: (fraction) => {
      if (!Number.isFinite(media.duration)) return;
      media.currentTime = fraction * media.duration;
    },
    onHover: (fraction, clientX) => {
      if (fraction == null || !Number.isFinite(media.duration)) {
        scrubTip.hidden = true;
        return;
      }
      const box = scrub.node.getBoundingClientRect();
      scrubTip.hidden = false;
      scrubTip.textContent = formatTime(fraction * media.duration);
      scrubTip.style.left = `${Math.min(box.width, Math.max(0, clientX - box.left))}px`;
    },
  });
  scrub.node.append(scrubTip);

  const volume = createTrack({
    cls: "vtrack--volume",
    label: "Volume",
    onScrub: (fraction) => {
      media.muted = false;
      media.volume = fraction;
    },
    onCommit: () => {
      persist(VOLUME_KEY, String(media.volume));
      persist(MUTED_KEY, media.muted ? "1" : "0");
    },
  });

  const speedBtn = el(
    "button",
    { type: "button", class: "vctl vctl--speed", "aria-label": "Playback speed", title: "Playback speed", onclick: cycleSpeed },
    ["1×"],
  );

  const pipBtn = ctrlButton("#i-pip", "Picture in picture", togglePip);
  const fsBtn = ctrlButton("#i-fs", "Fullscreen", () => toggleFullscreen());

  // Audio uses `.abar` (always-on, static, centered). Video uses `.vbar`
  // (overlay that can idle-hide, spacer pins PiP/FS to the right).
  const bar = el("div", { class: isVideo ? "vbar" : "abar" }, [
    scrub.node,
    el(
      "div",
      { class: isVideo ? "vbar__row" : "vbar__row vbar__row--center" },
      isVideo
        ? [
            el("div", { class: "vbar__group" }, [backBtn, playBtn, fwdBtn]),
            el("div", { class: "vbar__group vbar__group--volume" }, [muteBtn, volume.node]),
            timeLabel,
            el("div", { class: "vbar__spacer" }),
            el("div", { class: "vbar__group" }, [
              speedBtn,
              document.pictureInPictureEnabled ? pipBtn : null,
              fsBtn,
            ]),
          ]
        : [
            el("div", { class: "vbar__group" }, [backBtn, playBtn, fwdBtn]),
            el("div", { class: "vbar__group vbar__group--volume" }, [muteBtn, volume.node]),
            timeLabel,
            el("div", { class: "vbar__group" }, [speedBtn]),
          ],
    ),
  ]);

  const bigPlay = el(
    "button",
    { type: "button", class: "vplayer__bigplay", "aria-label": "Play", onclick: () => togglePlay() },
    [icon("#i-play", 30)],
  );
  const spinner = el("div", { class: "vplayer__spinner", hidden: true });
  const spinnerLabel = el("div", { class: "vplayer__spinner-label", hidden: true });
  const notice = el("div", { class: "vplayer__notice", hidden: true });

  // The <audio> element stays in the tree (hidden) so the lightbox teardown,
  // which pauses every media node under the scrim, still reaches it.
  const node = el(
    "div",
    { class: `vplayer vplayer--${kind}` },
    isVideo ? [media, bigPlay, spinner, spinnerLabel, notice, bar] : [media, notice, bar],
  );

  /* ---- behaviour ---- */
  function togglePlay() {
    if (media.ended) media.currentTime = 0;
    if (media.paused) media.play().catch(() => {});
    else media.pause();
  }

  function skip(seconds) {
    if (!Number.isFinite(media.duration)) return;
    media.currentTime = Math.min(media.duration, Math.max(0, media.currentTime + seconds));
    wake();
  }

  function toggleMute() {
    media.muted = !media.muted;
    persist(MUTED_KEY, media.muted ? "1" : "0");
  }

  function cycleSpeed() {
    const next = SPEEDS[(SPEEDS.indexOf(media.playbackRate) + 1) % SPEEDS.length] ?? 1;
    media.playbackRate = next;
    speedBtn.textContent = `${next}×`;
  }

  async function togglePip() {
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else await media.requestPictureInPicture();
    } catch {
      /* browser refused — nothing useful to say */
    }
  }

  function toggleFullscreen() {
    const target = fullscreenTarget || node;
    if (document.fullscreenElement) {
      document.exitFullscreen?.().catch(() => {});
    } else {
      (target.requestFullscreen || target.webkitRequestFullscreen)?.call(target);
    }
  }

  /** Seconds of contiguous buffer ahead of the playhead. */
  function bufferedAhead() {
    for (let i = 0; i < media.buffered.length; i += 1) {
      const start = media.buffered.start(i);
      const end = media.buffered.end(i);
      if (start <= media.currentTime + 0.25 && media.currentTime <= end) {
        return end - media.currentTime;
      }
    }
    return 0;
  }

  /** Bytes the browser has actually fetched, derived from buffered time spans. */
  function bufferedBytes() {
    if (!sizeBytes || !Number.isFinite(media.duration) || !media.duration) return 0;
    let seconds = 0;
    for (let i = 0; i < media.buffered.length; i += 1) {
      seconds += media.buffered.end(i) - media.buffered.start(i);
    }
    return (seconds / media.duration) * sizeBytes;
  }

  function showSpinner(label) {
    spinner.hidden = false;
    spinnerLabel.hidden = false;
    spinnerLabel.textContent = label;
    bigPlay.hidden = true;
  }

  function hideSpinner() {
    spinner.hidden = true;
    spinnerLabel.hidden = true;
  }

  /**
   * Hold playback until there's a real buffer cushion. Resolves early on
   * canplaythrough, and always resolves by PREROLL_TIMEOUT_MS so a slow link
   * degrades to "start and buffer" rather than an endless spinner.
   */
  function preroll() {
    // Audio is a compact always-on card — no overlay spinner, no pre-roll gate.
    if (!isVideo) return Promise.resolve();
    if (media.readyState >= 4 || bufferedAhead() >= PREROLL_SECONDS) return Promise.resolve();
    showSpinner("Buffering…");
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        clearInterval(poll);
        clearTimeout(cap);
        media.removeEventListener("canplaythrough", finish);
        hideSpinner();
        resolve();
      };
      const poll = setInterval(() => {
        if (bufferedAhead() >= PREROLL_SECONDS) finish();
      }, 250);
      const cap = setTimeout(finish, PREROLL_TIMEOUT_MS);
      media.addEventListener("canplaythrough", finish);
      timers.push(poll, cap);
    });
  }

  /**
   * Compare the file's average bitrate against how fast we're actually
   * fetching it. If the link can't keep up, say so plainly instead of
   * letting the user sit through repeated stalls.
   */
  function watchThroughput() {
    if (!sizeBytes || !isVideo) return;
    const startBytes = bufferedBytes();
    const started = performance.now();
    const timer = setTimeout(() => {
      if (!Number.isFinite(media.duration) || !media.duration) return;
      const elapsed = (performance.now() - started) / 1000;
      const gainedBits = (bufferedBytes() - startBytes) * 8;
      if (elapsed <= 0 || gainedBits <= 0) return;
      const throughput = gainedBits / elapsed;
      const required = (sizeBytes * 8) / media.duration;
      // Fully buffered files need no warning regardless of measured rate.
      if (bufferedAhead() + media.currentTime >= media.duration - 1) return;
      if (throughput >= required * 1.05) return;
      showNotice(
        `This video needs ~${formatBits(required)} to play smoothly but is loading at ~${formatBits(throughput)}. Expect stalls.`,
      );
    }, THROUGHPUT_SAMPLE_MS);
    timers.push(timer);
  }

  function showNotice(message) {
    notice.replaceChildren(
      el("span", { text: message }),
      onDownload
        ? el("button", { type: "button", class: "vctl vctl--wide", onclick: onDownload }, [icon("#i-download", 14), "Download"])
        : null,
      el("button", {
        type: "button",
        class: "vctl",
        "aria-label": "Dismiss",
        onclick: () => {
          notice.hidden = true;
        },
      }, [icon("#i-x", 14)]),
    );
    notice.hidden = false;
  }

  function bufferedFraction() {
    if (!Number.isFinite(media.duration) || !media.duration) return 0;
    for (let i = 0; i < media.buffered.length; i += 1) {
      if (media.buffered.start(i) <= media.currentTime && media.currentTime <= media.buffered.end(i)) {
        return media.buffered.end(i) / media.duration;
      }
    }
    return 0;
  }

  function syncProgress() {
    if (scrub.isDragging()) return;
    const fraction = Number.isFinite(media.duration) && media.duration ? media.currentTime / media.duration : 0;
    scrub.set(fraction, bufferedFraction());
    timeNow.textContent = formatTime(media.currentTime);
  }

  function syncPlayState() {
    const playing = !media.paused && !media.ended;
    node.classList.toggle("is-playing", playing);
    playBtn.replaceChildren(icon(playing ? "#i-pause" : "#i-play", 19));
    playBtn.setAttribute("aria-label", playing ? "Pause" : "Play");
    playBtn.title = playing ? "Pause" : "Play";
    // A paused player is never buffering — clear any stall spinner.
    if (!playing) spinner.hidden = true;
    if (isVideo) {
      bigPlay.hidden = playing || !spinner.hidden;
      if (playing) wake();
      else node.classList.remove("is-idle");
    }
  }

  function syncVolume() {
    const level = media.muted ? 0 : media.volume;
    volume.set(level);
    muteBtn.replaceChildren(icon(level === 0 ? "#i-vol-x" : "#i-vol", 17));
    muteBtn.setAttribute("aria-label", media.muted ? "Unmute" : "Mute");
    muteBtn.title = media.muted ? "Unmute" : "Mute";
  }

  function syncFullscreen() {
    const active = Boolean(document.fullscreenElement);
    fsBtn.replaceChildren(icon(active ? "#i-fs-x" : "#i-fs", 17));
    fsBtn.setAttribute("aria-label", active ? "Exit fullscreen" : "Fullscreen");
    fsBtn.title = active ? "Exit fullscreen" : "Fullscreen";
  }

  /*
   * Auto-hide chrome while playing; any pointer activity brings it back.
   * Video only — the audio card has nothing behind its controls to reveal,
   * and it registers no pointer listeners, so a hidden bar would never
   * come back.
   */
  let idleTimer = 0;
  function wake() {
    if (!isVideo) return;
    node.classList.remove("is-idle");
    clearTimeout(idleTimer);
    if (media.paused) return;
    idleTimer = setTimeout(() => node.classList.add("is-idle"), IDLE_HIDE_MS);
  }

  const listeners = [
    [media, "timeupdate", syncProgress],
    [media, "progress", syncProgress],
    [media, "seeking", syncProgress],
    [media, "seeked", syncProgress],
    [media, "play", syncPlayState],
    [media, "pause", syncPlayState],
    [media, "ended", syncPlayState],
    [media, "volumechange", syncVolume],
    [media, "ratechange", () => {
      speedBtn.textContent = `${media.playbackRate}×`;
    }],
    [media, "loadedmetadata", () => {
      timeTotal.textContent = formatTime(media.duration);
      syncProgress();
    }],
    // Only spin for a stall during playback — a paused video shows its
    // play button instead, and stacking both reads as a glitch.
    [media, "waiting", () => {
      if (media.paused) return;
      showSpinner("Buffering…");
    }],
    [media, "playing", () => {
      hideSpinner();
      syncPlayState();
    }],
    [media, "canplay", () => {
      hideSpinner();
      syncPlayState();
    }],
    [media, "error", () => {
      hideSpinner();
      if (onError) onError();
    }],
    [document, "fullscreenchange", syncFullscreen],
  ];

  if (isVideo) {
    // Click the frame to play/pause, double-click for fullscreen.
    listeners.push(
      [media, "click", () => togglePlay()],
      [media, "dblclick", () => toggleFullscreen()],
      [node, "pointermove", wake],
      [node, "pointerleave", () => {
        if (!media.paused) node.classList.add("is-idle");
      }],
    );
  }

  for (const [target, event, handler] of listeners) target.addEventListener(event, handler);

  syncPlayState();
  syncVolume();
  syncProgress();
  syncFullscreen();

  /** Buffer a cushion, then begin playback. Falls back to paused if blocked. */
  async function start() {
    await preroll();
    try {
      await media.play();
      watchThroughput();
    } catch {
      /* autoplay blocked — the big play button is right there */
      syncPlayState();
    }
  }

  function destroy() {
    clearTimeout(idleTimer);
    for (const timer of timers) {
      clearTimeout(timer);
      clearInterval(timer);
    }
    for (const [target, event, handler] of listeners) target.removeEventListener(event, handler);
    try {
      media.pause();
    } catch {
      /* ignore */
    }
    if (document.pictureInPictureElement === media) document.exitPictureInPicture?.().catch(() => {});
    media.removeAttribute("src");
    media.load();
  }

  return { node, media, destroy, start, togglePlay, skip, toggleFullscreen, toggleMute };
}
