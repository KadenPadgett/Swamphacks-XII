let base = "";
let token = "";
export function configureApi(url, writeToken = "") {
  base = url.trim().replace(/\/$/, "");
  token = writeToken;
}
export function apiUrl(path) {
  return `${base}${path}`;
}
async function requestOnce(path, options = {}) {
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
    const { retries, timeout, ...fetchOptions } = options;
    const res = await fetch(apiUrl(path), {
      ...fetchOptions,
      headers,
      signal: controller.signal,
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const error = new Error(
        typeof data?.detail === "string"
          ? data.detail
          : Array.isArray(data?.detail)
            ? data.detail.map((e) => e.msg).join("; ")
            : `Request failed (${res.status}).`,
      );
      error.status = res.status;
      throw error;
    }
    if (data === null)
      throw new Error(
        "The server did not return JSON. Check the backend address.",
      );
    return data;
  } catch (err) {
    if (err.name === "AbortError") {
      const failure = new Error(options.signal?.aborted
        ? "Request cancelled."
        : "The request timed out. Previously loaded data may be stale.");
      failure.retryable = !options.signal?.aborted;
      throw failure;
    }
    if (err instanceof TypeError) err.retryable = true;
    throw err;
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
  }
}
export async function request(path, options = {}) {
  const attempts = options.method && options.method !== "GET" ? 1 : 1 + (options.retries || 0);
  let failure;
  for (let attempt = 0; attempt < attempts; attempt++) {
    if (options.signal?.aborted) throw new Error("Request cancelled.");
    try {
      return await requestOnce(path, options);
    } catch (error) {
      failure = error;
      const retryable = error.retryable || [429, 502, 503, 504].includes(error.status);
      if (!retryable || attempt === attempts - 1 || options.signal?.aborted) throw error;
      await new Promise((resolve) => {
        const done = () => {
          clearTimeout(timer);
          options.signal?.removeEventListener("abort", done);
          resolve();
        };
        const timer = setTimeout(done, 500 * 2 ** attempt);
        options.signal?.addEventListener("abort", done, { once: true });
      });
    }
  }
  throw failure;
}
export const post = (path, body = {}, extra = {}) =>
  request(path, { method: "POST", body: JSON.stringify(body), ...extra });

// Render's free instances can take time to wake. Keep the entire attempt bounded.
export async function connectWithRetry({ timeoutMs = 90000, signal } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  let attempt = 0;
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
        const timer = setTimeout(done, Math.min(1000 * 2 ** Math.min(attempt++, 3), remaining));
        signal?.addEventListener("abort", done, { once: true });
      });
    }
  }
  if (signal?.aborted) throw new Error("Connection cancelled.");
  throw new Error(`Could not connect to the backend within ${Math.round(timeoutMs / 1000)} seconds. ${lastError?.message || "Please try again."}`);
}
