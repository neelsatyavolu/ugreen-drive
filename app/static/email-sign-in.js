/** Email-code login, preserving the current Drive folder on successful sign-in. */
export function bindEmailSignIn() {
  const form = document.getElementById("email-sign-in-form");
  if (!form || form.dataset.bound === "1") return;
  form.dataset.bound = "1";
  const email = document.getElementById("sign-in-email");
  const code = document.getElementById("sign-in-email-code");
  const codeWrap = document.getElementById("email-code-wrap");
  const status = document.getElementById("email-sign-in-status");
  const error = document.getElementById("email-sign-in-error");
  const submit = document.getElementById("email-sign-in-submit");
  const actions = document.getElementById("email-code-actions");
  const resend = document.getElementById("email-code-resend");
  const change = document.getElementById("email-code-change");
  let codeSent = false;
  let busy = false;
  let resendAt = 0;

  function showError(message) {
    error.textContent = message;
    error.hidden = !message;
  }

  async function send(action) {
    if (busy) return;
    busy = true;
    showError("");
    for (const control of [email, code, submit, resend, change]) control.disabled = true;
    submit.textContent = action === "request" ? "Sending code…" : "Signing in…";
    try {
      const response = await fetch(`/auth/email/${action}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.value.trim(), ...(action === "verify" ? { code: code.value.trim() } : {}) }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(typeof payload.detail === "string" ? payload.detail : "Check your email address and six-digit code.");
      }
      if (action === "verify") {
        location.reload();
        return;
      }
      codeSent = true;
      resendAt = Date.now() + 60_000;
      code.value = "";
      code.required = true;
      codeWrap.hidden = false;
      status.hidden = false;
      actions.hidden = false;
    } catch (err) {
      showError(err instanceof Error ? err.message : "Could not sign you in. Please try again.");
    } finally {
      busy = false;
      for (const control of [email, code, submit, resend, change]) control.disabled = false;
      email.readOnly = codeSent;
      submit.textContent = codeSent ? "Sign in" : "Sign in with email";
      if (codeSent) code.focus();
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (form.reportValidity()) void send(codeSent ? "verify" : "request");
  });
  resend.addEventListener("click", () => {
    if (Date.now() < resendAt) {
      showError("Please wait a minute before requesting another code.");
      return;
    }
    void send("request");
  });
  change.addEventListener("click", () => {
    codeSent = false;
    code.value = "";
    code.required = false;
    codeWrap.hidden = true;
    status.hidden = true;
    actions.hidden = true;
    email.readOnly = false;
    submit.textContent = "Sign in with email";
    showError("");
    email.focus();
  });
}
