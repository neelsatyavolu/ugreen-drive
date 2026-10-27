import { el } from "./dom.js?v=20261026-ugd1";
import { marked } from "./vendor/marked.js?v=20261026-ugd1";
import DOMPurify from "./vendor/dompurify.js?v=20261026-ugd1";

export function isMarkdown(name) {
  return /\.(md|markdown)$/i.test(name);
}

/** Add rendered/raw modes while preserving the existing source and controls. */
export function enhanceMarkdownPreview({ container, raw, bar, text, wrapButton }) {
  const preview = el("article", { class: "markdown", "aria-label": "Markdown preview", tabindex: "0" });
  preview.append(DOMPurify.sanitize(marked.parse(text.replace(/^\uFEFF/, ""), { gfm: true }), {
    RETURN_DOM_FRAGMENT: true,
    ALLOWED_TAGS: ["p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "del", "s", "blockquote", "ul", "ol", "li", "pre", "code", "a", "img", "table", "thead", "tbody", "tr", "th", "td", "input", "sup", "sub", "details", "summary"],
    ALLOWED_ATTR: ["href", "src", "alt", "title", "start", "type", "checked", "disabled", "align"],
  }));
  for (const link of preview.querySelectorAll("a")) {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  for (const input of preview.querySelectorAll("input")) {
    input.type = "checkbox";
    input.disabled = true;
  }
  for (const img of preview.querySelectorAll("img")) {
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
  }
  const previewButton = el("button", { type: "button", class: "vctl vctl--wide", text: "Preview" });
  const rawButton = el("button", { type: "button", class: "vctl vctl--wide", text: "Raw" });
  const setMode = (rendered) => {
    raw.hidden = rendered;
    preview.hidden = !rendered;
    if (wrapButton) wrapButton.hidden = rendered;
    for (const [button, selected] of [[previewButton, rendered], [rawButton, !rendered]]) {
      button.classList.toggle("is-on", selected);
      button.setAttribute("aria-pressed", String(selected));
    }
  };
  previewButton.addEventListener("click", () => setMode(true));
  rawButton.addEventListener("click", () => setMode(false));
  bar.prepend(el("div", { class: "markdown__modes", role: "group", "aria-label": "Markdown view" }, [previewButton, rawButton]));
  container.insertBefore(preview, raw);
  setMode(true);
}
