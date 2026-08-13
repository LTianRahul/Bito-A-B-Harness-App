// Tiny typed fetch wrapper + SSE helper. All endpoints are under /api.

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function handle<T>(res: Response): Promise<T> {
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    throw new ApiError(res.status, (data && (data.detail ?? data)) ?? res.statusText);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => fetch(`/api${path}`).then((r) => handle<T>(r)),

  post: <T>(path: string, body?: unknown) =>
    fetch(`/api${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then((r) => handle<T>(r)),

  put: <T>(path: string, body?: unknown) =>
    fetch(`/api${path}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then((r) => handle<T>(r)),

  del: <T>(path: string) =>
    fetch(`/api${path}`, { method: "DELETE" }).then((r) => handle<T>(r)),
};

// Subscribe to a Server-Sent Events stream. Returns an unsubscribe function.
export function subscribe(
  path: string,
  onEvent: (data: any) => void,
  onError?: (e: Event) => void,
): () => void {
  const es = new EventSource(`/api${path}`);
  es.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data));
    } catch {
      onEvent(e.data);
    }
  };
  if (onError) es.onerror = onError;
  return () => es.close();
}
