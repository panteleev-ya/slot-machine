const DEFAULTS = { piUrl: "http://yarikos.local:8080", token: "" };
const $ = (id) => document.getElementById(id);

function show(text, cls) {
  $("saved").textContent = text;
  $("saved").className = cls;
}

async function load() {
  const { piUrl, token } = await chrome.storage.local.get(DEFAULTS);
  $("piUrl").value = piUrl;
  $("token").value = token;
}

async function save() {
  let url;
  try {
    url = new URL($("piUrl").value.trim() || DEFAULTS.piUrl);
  } catch {
    return show("That isn't a URL. Use the RENEW_URL from the settings, like http://yarikos.local:8080.", "bad");
  }
  if (url.protocol !== "http:") {
    return show("Use the http:// address from RENEW_URL.", "bad");
  }
  const token = $("token").value.trim();
  if (!/^[A-Za-z0-9_-]{32,}$/.test(token)) {
    return show("That doesn't look like a RENEW_TOKEN. Run make renew-token and paste it again.", "bad");
  }
  // yarikos.local is granted at install; any other address needs your OK once
  if (url.hostname !== "yarikos.local") {
    const granted = await chrome.permissions.request({ origins: [`http://${url.hostname}/*`] });
    if (!granted) return show(`Not saved: the extension needs access to ${url.hostname}.`, "bad");
  }
  await chrome.storage.local.set({ piUrl: url.origin, token });
  show("Saved.", "ok");
}

$("save").addEventListener("click", save);
load();
