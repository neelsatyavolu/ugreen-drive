/**
 * Shared DOM helpers used by app.js and viewer.js.
 * Kept dependency-free so any module can import it without cycles.
 */
const SVG_NS = "http://www.w3.org/2000/svg";

export const $ = (id) => document.getElementById(id);

/**
 * Build an element. `attrs` supports class/text, on* listeners, and plain
 * attributes; `true` renders as a bare attribute, null/false are skipped.
 */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of [].concat(children)) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Reference a symbol from the inline sprite in index.html. */
export function icon(symbol, size = 15, style = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  if (style) svg.setAttribute("style", style);
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", symbol);
  svg.append(use);
  return svg;
}

export function show(node, visible) {
  node.hidden = !visible;
}
