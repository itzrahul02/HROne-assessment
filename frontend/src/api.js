let rawListener = () => {};

export function onRaw(listener) {
  rawListener = listener;
}

export function formatError(body) {
  if (!body) return "Request failed";
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) {
    return body.detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
  }
  return JSON.stringify(body);
}

export async function request(path, options = {}, quiet = false) {
  const response = await fetch(path, options);
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { detail: text };
    }
  }
  if (!quiet) rawListener(`${options.method || "GET"} ${path}`, body);
  if (!response.ok) {
    const error = new Error(formatError(body));
    error.status = response.status;
    throw error;
  }
  return body;
}

export function get(path, quiet = false) {
  return request(path, {}, quiet);
}

export function send(method, path, payload) {
  return request(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function query(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== "" && value != null) search.set(key, value);
  });
  const text = search.toString();
  return text ? `?${text}` : "";
}

export function istToEpoch(value) {
  if (!value) return null;
  const withSeconds = value.length === 16 ? `${value}:00` : value;
  const ms = Date.parse(`${withSeconds}+05:30`);
  if (Number.isNaN(ms)) throw new Error("Could not read that time.");
  return ms;
}

export function formatIst(ms) {
  if (ms == null) return "—";
  const text = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(new Date(ms));
  return `${text} IST`;
}
