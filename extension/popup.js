// Reads the visa site's session cookie (only after you signed in yourself) and sends it
// to the slot-machine Pi. The Pi never sends a cookie back; nothing is stored here but
// the Pi's address and token (options).
const AIS = "https://ais.usvisa-info.com";
const DEFAULTS = { piUrl: "http://yarikos.local:8080", token: "" };
const UNREACHABLE = "Can't reach the Pi. Are you on the home Wi-Fi, and is the page on (make status)?";

const $ = (id) => document.getElementById(id);

function show(id, text, cls = "") {
  const el = $(id);
  el.textContent = text; // plain text only: it comes from the network
  el.className = cls;
}

async function settings() {
  return chrome.storage.local.get(DEFAULTS);
}

async function callPi(path, init = {}) {
  const { piUrl, token } = await settings();
  const res = await fetch(piUrl.replace(/\/+$/, "") + path, {
    ...init,
    headers: { ...(init.headers || {}), Authorization: `Bearer ${token}` },
    signal: AbortSignal.timeout(60000), // a check takes a few seconds; the site can be slow
  });
  let data;
  try {
    data = await res.json();
  } catch {
    data = { ok: false, message: `The Pi answered HTTP ${res.status}.` };
  }
  return { status: res.status, ...data };
}

async function sessionCookie() {
  const cookie = await chrome.cookies.get({ url: `${AIS}/`, name: "_yatri_session" });
  return cookie ? cookie.value : null;
}

async function init() {
  $("options").addEventListener("click", (e) => {
    e.preventDefault();
    chrome.runtime.openOptionsPage();
  });
  $("send").addEventListener("click", send);

  const { token } = await settings();
  if (!token) {
    show("status", "Paste the Pi's token in Options first (make renew-token copies it).", "bad");
    $("send").disabled = true;
    return;
  }
  try {
    const st = await callPi("/api/status");
    if (st.status === 401) {
      show("status", "The Pi rejected the token. Paste the current one in Options.", "bad");
    } else if (st.ok) {
      const earliest = st.earliest ? ` · earliest ${st.earliest}` : "";
      show("status", `Session on the Pi: ${st.session}${earliest}`, st.expired ? "bad" : "muted");
      if (st.sign_in_url) $("signin").href = st.sign_in_url;
    } else {
      show("status", st.message || `The Pi answered HTTP ${st.status}.`, "bad");
    }
  } catch {
    show("status", UNREACHABLE, "bad");
  }
  if (await sessionCookie()) {
    // the site sets this cookie as soon as its sign-in page opens, so it doesn't prove you're signed in
    show("cookie", "Visa-site cookie found. Send it once you're signed in and see your appointment page.", "muted");
  } else {
    show("cookie", "No visa-site cookie in this browser yet: sign in first.", "bad");
    $("send").disabled = true;
  }
}

async function send() {
  $("send").disabled = true;
  show("result", "Sending and checking…", "muted");
  try {
    const cookie = await sessionCookie();
    if (!cookie) {
      show("result", "No visa-site session in this browser. Sign in first.", "bad");
      return;
    }
    const r = await callPi("/api/cookie", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // the site only accepts the session from the same browser version, so the Pi copies this one
      body: JSON.stringify({ cookie, user_agent: navigator.userAgent }),
    });
    show("result", r.message || `The Pi answered HTTP ${r.status}.`, r.ok ? "ok" : "bad");
  } catch {
    show("result", UNREACHABLE, "bad");
  } finally {
    $("send").disabled = false;
  }
}

init();
