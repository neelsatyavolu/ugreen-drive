/**
 * Admin Settings: drive name and addresses, sign-in methods, linked sign-ins.
 * NAS administrators only (the server checks too). Saved to /config/settings.json.
 */
import * as api from "./api.js?v=20261026-ugd1";
import { ApiError } from "./api.js?v=20261026-ugd1";
import { el, icon } from "./dom.js?v=20261026-ugd1";

const SECRET_KEYS = ["google_client_secret", "resend_api_key"];

function field(label, input, hint) {
  return el("label", { class: "settings-field" }, [
    el("span", { class: "settings-field__label", text: label }),
    input,
    hint ? el("span", { class: "settings-field__hint", text: hint }) : null,
  ]);
}

function textInput(name, value, { placeholder = "", type = "text" } = {}) {
  return el("input", {
    class: "field",
    name,
    type,
    value: value || "",
    placeholder,
    autocomplete: "off",
    spellcheck: "false",
    autocapitalize: "off",
  });
}

/** Secret inputs never show the saved value; blank keeps it, the checkbox removes it. */
function secretInput(name, saved) {
  const input = textInput(name, "", { type: "password", placeholder: saved ? "Saved (leave blank to keep)" : "" });
  const clear = saved
    ? el("label", { class: "settings-check" }, [
        el("input", { type: "checkbox", name: `clear:${name}` }),
        el("span", { text: "Remove the saved value" }),
      ])
    : null;
  return el("div", { class: "settings-secret" }, [input, clear]);
}

function section(title, lede, children) {
  return el("section", { class: "settings-section" }, [
    el("h4", { class: "settings-section__title", text: title }),
    lede ? el("p", { class: "settings-section__lede", text: lede }) : null,
    ...children,
  ]);
}

function userSelect(users, selected) {
  return el(
    "select",
    { class: "field settings-user" },
    users.map((name) => el("option", { value: name, text: name, selected: name === selected })),
  );
}

function peopleEditor(identities, users) {
  const list = el("div", { class: "settings-people" });
  const rows = [];

  function addRow(email, username) {
    const select = userSelect(users, username);
    const remove = el("button", { type: "button", class: "icon-btn", "aria-label": `Remove ${email}` }, [icon("#i-x", 14)]);
    const row = el("div", { class: "settings-person" }, [
      el("code", { class: "mono settings-person__email", text: email }),
      el("span", { class: "settings-person__arrow", text: "→" }),
      select,
      remove,
    ]);
    const entry = { email, select, row };
    remove.addEventListener("click", () => {
      rows.splice(rows.indexOf(entry), 1);
      row.remove();
    });
    rows.push(entry);
    list.append(row);
  }

  for (const [email, username] of Object.entries(identities).sort()) addRow(email, username);

  const newEmail = textInput("new-email", "", { type: "email", placeholder: "name@example.com" });
  const newUser = userSelect(users, users[0]);
  const add = el("button", { type: "button", class: "btn btn--modal", text: "Link" });
  const error = el("p", { class: "settings-error", hidden: true });
  add.addEventListener("click", () => {
    const email = newEmail.value.trim().toLowerCase();
    error.hidden = true;
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      error.textContent = "Enter an email address.";
      error.hidden = false;
      return;
    }
    if (rows.some((r) => r.email === email)) {
      error.textContent = `${email} is already linked.`;
      error.hidden = false;
      return;
    }
    addRow(email, newUser.value);
    newEmail.value = "";
  });

  const adder = users.length
    ? el("div", { class: "settings-person settings-person--new" }, [newEmail, el("span", { class: "settings-person__arrow", text: "→" }), newUser, add])
    : el("p", { class: "settings-field__hint", text: "No NAS accounts found. Create one in UGOS first." });

  return {
    node: el("div", {}, [list, adder, error]),
    value: () => Object.fromEntries(rows.map((r) => [r.email, r.select.value])),
  };
}

function readValues(form) {
  const data = new FormData(form);
  const values = {};
  const clear = [];
  for (const key of ["drive_name", "public_base_url", "lan_origin", "default_share", "google_client_id", "resend_from_email"]) {
    values[key] = String(data.get(key) ?? "").trim();
  }
  for (const key of SECRET_KEYS) {
    const value = String(data.get(key) ?? "").trim();
    if (value) values[key] = value;
    if (data.get(`clear:${key}`)) clear.push(key);
  }
  return { ...values, clear };
}

function buildForm(view, shares) {
  const v = view.values;
  const eff = view.effective;
  const shareOptions = [el("option", { value: "", text: "First one each person can open" })].concat(
    shares.filter((s) => s.id).map((s) => el("option", { value: s.id, text: s.name || s.id, selected: s.id === v.default_share })),
  );
  const people = peopleEditor(view.identities || {}, view.nas_users || []);
  const methods = view.methods || {};

  const form = el("form", { class: "settings-form", novalidate: true }, [
    section("General", null, [
      field("Name", textInput("drive_name", v.drive_name, { placeholder: "Drive" }), "Shown on the sign-in page, in the tab title and in emails."),
      field("Public address", textInput("public_base_url", v.public_base_url, { placeholder: "https://drive.example.com" }),
        `The address you use away from home (Cloudflare Tunnel). Now: ${eff.public_base_url}`),
      field("LAN address", textInput("lan_origin", v.lan_origin, { placeholder: eff.lan_origin || "http://192.168.1.50:8790" }),
        "Browsers on the NAS's network switch here automatically, skipping the tunnel. Leave empty to detect it."),
      field("Open first", el("select", { class: "field", name: "default_share" }, shareOptions)),
    ]),
    section("Sign-in methods", "NAS username and password always works. Turn on more ways to sign in here, then link each email under People.", [
      el("h5", { class: "settings-sub", text: `Google ${methods.google ? "· on" : "· off"}` }),
      field("Client ID", textInput("google_client_id", v.google_client_id, { placeholder: "…apps.googleusercontent.com" })),
      field("Client secret", secretInput("google_client_secret", v.google_client_secret?.set),
        `In Google Cloud → APIs & Services → Credentials, create an OAuth web client with this redirect URI: ${eff.google_redirect_uri}`),
      el("h5", { class: "settings-sub", text: `Email codes ${methods.email ? "· on" : "· off"}` }),
      field("Resend API key", secretInput("resend_api_key", v.resend_api_key?.set), "From resend.com, with a verified sending domain."),
      field("From", textInput("resend_from_email", v.resend_from_email, { placeholder: "Drive <drive@example.com>" })),
    ]),
    section("People", "Each linked email signs in as that NAS account, with its UGOS permissions. To add a new person, create their account in UGOS → Control Panel → User, then link it here.", [people.node]),
    el("p", { class: "settings-error", hidden: true, "data-role": "error" }),
  ]);

  return { form, people };
}

/**
 * Open the Settings modal. `ui` is the app's modal/toast helpers:
 * { openModal, closeModal, modalFooter, toast, shares, onSaved }.
 */
export async function openSettingsModal(ui) {
  let view;
  try {
    view = await api.getAdminSettings();
  } catch (e) {
    ui.toast(e instanceof ApiError ? e.message : "Couldn't load settings.", "error");
    return;
  }
  const { form, people } = buildForm(view, ui.shares || []);
  const error = form.querySelector('[data-role="error"]');
  const save = el("button", { type: "submit", class: "btn btn--primary btn--modal", text: "Save" });

  async function submit(event) {
    event?.preventDefault();
    error.hidden = true;
    save.disabled = true;
    try {
      const saved = await api.saveAdminSettings({ values: readValues(form), identities: people.value() });
      ui.closeModal();
      ui.toast("Settings saved");
      ui.onSaved?.(saved);
    } catch (e) {
      error.textContent = e instanceof ApiError ? e.message : "Couldn't save settings.";
      error.hidden = false;
    } finally {
      save.disabled = false;
    }
  }
  form.addEventListener("submit", submit);
  form.append(ui.modalFooter([el("button", { type: "button", class: "btn btn--modal", text: "Cancel", onclick: ui.closeModal }), save]));

  const body = el("div", { class: "modal__body" }, [
    el("div", { class: "modal__lead" }, [
      el("div", { class: "modal__badge" }, [icon("#i-gear", 17)]),
      el("div", {}, [
        el("h3", { text: "Settings" }),
        el("p", { text: "Only NAS administrators see this. Changes apply right away." }),
      ]),
    ]),
    form,
  ]);
  ui.openModal(el("div", { class: "modal modal--settings" }, [body]));
}
