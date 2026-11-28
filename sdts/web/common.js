/* Shared shell: API client, small DOM helpers, formatting.
 *
 * Kept intentionally tiny - three pages share it and none of them needs a
 * framework. Everything here is a pure function or a thin fetch wrapper, so the
 * pages stay readable and there is no state to get out of sync between tabs.
 */

/** Fetch JSON, turning an HTTP error into an Error with the server's `detail`. */
export async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {} };
  if (form) {
    opts.body = form;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    throw new Error("network");
  }
  const type = res.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    const detail = typeof payload === "object" && payload ? payload.detail : payload;
    const error = new Error(detail || `HTTP ${res.status}`);
    error.status = res.status;
    error.detail = detail;
    throw error;
  }
  return payload;
}

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** 3661.42 -> "1:01:01.4"; under an hour it drops the leading 0:. */
export function fmtClock(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = (s % 60).toFixed(1).padStart(4, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

/** 95.4 -> "1 min 35 s" in the current language's short units. */
export function fmtDuration(seconds, units) {
  const s = Math.max(0, Number(seconds) || 0);
  if (s < 60) return `${s.toFixed(0)} ${units.seconds}`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s % 60);
  return rest ? `${m} ${units.minutes} ${rest} ${units.seconds}` : `${m} ${units.minutes}`;
}

export function fmtBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

/** Long paths are unreadable in a table cell; keep head and tail. */
export function shortenPath(path, keep = 34) {
  if (!path || path.length <= keep * 2) return path || "";
  return `${path.slice(0, keep)}…${path.slice(-keep)}`;
}

export function debounce(fn, ms = 250) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/** Human-readable message for an Error from `api()`. */
export function errorText(err, t) {
  if (err && err.message === "network") return t("error.network");
  if (err && err.status === 403) return t("error.forbidden");
  return (err && (err.detail || err.message)) || String(err);
}

/** Toast-ish inline banner, appended into a host element. */
export function banner(host, text, kind = "error") {
  const node = el("div", { class: `banner ${kind}`, text });
  host.prepend(node);
  setTimeout(() => node.classList.add("fade"), 4000);
  setTimeout(() => node.remove(), 4800);
  return node;
}

export function statusClass(status) {
  return `status status-${status}`;
}

export const POLL_MS = 1500;
