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
  const abort = () => controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timeout = setTimeout(
    () => controller.abort(),
    options.timeout ?? 20000,
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
      signal: controller.signal,
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
    options.signal?.removeEventListener("abort", abort);
  }
}
export const post = (path, body = {}, extra = {}) =>
  request(path, { method: "POST", body: JSON.stringify(body), ...extra });

// Render's free instances can take time to wake. Keep the entire attempt bounded.
export async function connectWithRetry({ timeoutMs = 90000, signal } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (!signal?.aborted && Date.now() < deadline) {
    try {
      await request("/api/health", {
        timeout: Math.min(15000, deadline - Date.now()), signal,
      });
      const result = await request("/api/risks", {
        timeout: Math.min(15000, Math.max(1, deadline - Date.now())), signal,
      });
      if (!Array.isArray(result.sites))
        throw new Error("This address did not return the PlanIT API contract.");
      return result;
    } catch (error) {
      if (signal?.aborted) throw error;
      lastError = error;
      // A responding server with an incompatible API will not improve by retrying.
      if (error.message.includes("PlanIT API contract")) throw error;
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      await new Promise((resolve) => {
        const done = () => {
          clearTimeout(timer);
          signal?.removeEventListener("abort", done);
          resolve();
        };
        const timer = setTimeout(done, Math.min(2000, remaining));
        signal?.addEventListener("abort", done, { once: true });
      });
    }
  }
  if (signal?.aborted) throw new Error("Connection cancelled.");
  throw new Error(`Could not connect to the backend within ${Math.round(timeoutMs / 1000)} seconds. ${lastError?.message || "Please try again."}`);
}
