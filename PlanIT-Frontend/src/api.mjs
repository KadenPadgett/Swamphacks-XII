let base = "";
let token = "";
export function configureApi(url, writeToken = "") {
  base = url.trim().replace(/\/$/, "");
  token = writeToken;
}
export function apiUrl(path) {
  return `${base}${path}`;
}
export async function request(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    options.timeout || 20000,
  );
  try {
    const headers = {
      ...(options.body instanceof Blob
        ? { "Content-Type": options.body.type }
        : options.body
          ? { "Content-Type": "application/json" }
          : {}),
      ...options.headers,
    };
    if (token && options.method && options.method !== "GET")
      headers.Authorization = `Bearer ${token}`;
    const res = await fetch(apiUrl(path), {
      ...options,
      headers,
      signal: options.signal || controller.signal,
    });
    const data = await res.json().catch(() => null);
    if (!res.ok)
      throw new Error(
        typeof data?.detail === "string"
          ? data.detail
          : Array.isArray(data?.detail)
            ? data.detail.map((e) => e.msg).join("; ")
            : `Request failed (${res.status}).`,
      );
    if (data === null)
      throw new Error(
        "The server did not return JSON. Check the backend address.",
      );
    return data;
  } catch (err) {
    if (err.name === "AbortError")
      throw new Error(
        "The request timed out. Your saved data has not been replaced.",
      );
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}
export const post = (path, body = {}, extra = {}) =>
  request(path, { method: "POST", body: JSON.stringify(body), ...extra });
