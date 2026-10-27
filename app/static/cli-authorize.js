// Consent page for the Mac app and `ugd login`. Allow/Deny is a plain form
// POST; the drive answers with a 303 to the app's 127.0.0.1 listener, so the
// one-time code is never readable by page script.

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const request = {
  port: params.get("port") || "",
  state: params.get("state") || "",
  challenge: params.get("challenge") || "",
  device: params.get("device") || "",
};

function applyTheme() {
  let light = false;
  try {
    light = localStorage.getItem("ifd-theme") === "light";
  } catch {
    light = false;
  }
  document.documentElement.classList.toggle("light", light);
}

function show(id) {
  for (const section of ["cli-loading", "cli-signin", "cli-consent", "cli-error"]) {
    $(section).hidden = section !== id;
  }
}

function fail(message) {
  $("cli-error-text").textContent = message;
  show("cli-error");
}

function looksValid() {
  const port = Number(request.port);
  return (
    Number.isInteger(port) &&
    port >= 1024 &&
    port <= 65535 &&
    /^[A-Za-z0-9_-]{16,128}$/.test(request.state) &&
    /^[A-Za-z0-9_-]{43}$/.test(request.challenge)
  );
}

async function getJSON(url) {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}

async function refresh() {
  let me;
  let code;
  try {
    [me, code] = await Promise.all([
      getJSON("/api/me"),
      getJSON(`/api/cli/confirm-code?state=${encodeURIComponent(request.state)}`),
    ]);
  } catch {
    fail("Couldn't reach the drive. Check your connection and reload this page.");
    return;
  }
  if (!me.authenticated) {
    show("cli-signin");
    return;
  }
  $("cli-device").textContent = `The Mac app on ${request.device.trim() || "an unnamed device"}`;
  $("cli-user").textContent = me.nas_username;
  $("cli-code").textContent = code.code;
  $("cli-port").value = request.port;
  $("cli-state").value = request.state;
  $("cli-challenge").value = request.challenge;
  $("cli-device-field").value = request.device;
  show("cli-consent");
}

applyTheme();
if (!looksValid()) {
  fail("This link is incomplete or broken. Click Sign in in the Mac app again to get a fresh one.");
} else {
  $("cli-consent").addEventListener("submit", () => {
    // Prevent double submits; the browser still sends the clicked button's value.
    setTimeout(() => {
      for (const button of $("cli-consent").querySelectorAll("button")) button.disabled = true;
    });
  });
  // Signing in may happen in another tab; re-check when this one is shown again.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !$("cli-signin").hidden) refresh();
  });
  refresh();
}
