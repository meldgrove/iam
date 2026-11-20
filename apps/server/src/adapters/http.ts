import { noteProgress } from "../worker/liveness";

/**
 * The HTTP client of every adapter.
 *
 * A read (GET or HEAD) that a rate limit, a 502, 503 or 504, or a refused
 * connection stops is tried up to twice more, after the pause the server
 * asks for or a short one, so one hiccup in a long listing does not fail
 * the whole read. A request that timed out is not tried again: the next try
 * would wait as long. Changes are never sent twice here: whether a change
 * landed is unknown, and the adapters handle a lost answer themselves.
 * Every answer counts as progress for the worker's watchdog. Redirects are
 * never followed.
 */

/** What the client sends requests with: `fetch`, or a fake in tests. */
export type FetchLike = (request: Request) => Promise<Response>;

/** Answers to a read that a moment later can turn out fine. */
export const RETRY_STATUSES = new Set([429, 502, 503, 504]);
/** Pauses before the second and the third try of a read, in seconds. */
export const RETRY_PAUSES = [1, 4];
/**
 * The pauses of one request add up to at most this, which also bounds how
 * long a page that reads a connected app waits. A server that asks for more
 * gets it from the job's or the read's own retry, later.
 */
export const MAX_RETRY_WAIT_SECONDS = 30;

/** Replaced in tests, which record the pauses instead of waiting. */
export const timing = {
  pause: (seconds: number) => new Promise<void>((resolve) => setTimeout(resolve, seconds * 1000)),
};

/** The request did not get an answer in time. */
export class HttpTimeoutError extends Error {
  constructor() {
    super("The request timed out");
    this.name = "HttpTimeoutError";
  }
}

/** The request did not reach the server, or its answer broke off. */
export class HttpTransportError extends Error {
  constructor(
    message: string,
    /** Nothing reached the server: the connection was refused or never made. */
    readonly connect: boolean,
  ) {
    super(message);
    this.name = "HttpTransportError";
  }
}

/** An answer, read whole. */
export class HttpResponse {
  constructor(
    readonly status: number,
    readonly headers: Headers,
    readonly text: string,
    readonly url: string,
  ) {}

  get ok(): boolean {
    return this.status >= 200 && this.status < 300;
  }

  /** The body as JSON; throws for a body that is none. */
  json<T = unknown>(): T {
    return JSON.parse(this.text) as T;
  }
}

/** The pause a Retry-After header asks for, in seconds. */
export function retryAfter(headers: Headers): number | null {
  const value = (headers.get("retry-after") ?? "").trim();
  if (!value) return null;
  if (/^[0-9]+$/.test(value)) return Number(value);
  const when = Date.parse(value);
  if (Number.isNaN(when) || !/[a-z]/i.test(value)) return null;
  return Math.max((when - Date.now()) / 1000, 0);
}

export type RequestOptions = {
  /** Query parameters; arrays repeat the parameter. Undefined values are left out. */
  params?: Record<string, string | number | boolean | undefined | (string | number)[]>;
  /** A JSON body. */
  json?: unknown;
  /** A form body (application/x-www-form-urlencoded). */
  form?: Record<string, string>;
  /** A raw body. */
  body?: string;
  headers?: Record<string, string>;
  /** Seconds; the client's timeout otherwise. */
  timeout?: number;
};

const CONNECT_CODES = new Set([
  "ECONNREFUSED",
  "ENOTFOUND",
  "EAI_AGAIN",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "UND_ERR_CONNECT_TIMEOUT",
  "UND_ERR_SOCKET",
]);

function errorCode(error: unknown): string | undefined {
  let current: unknown = error;
  for (let depth = 0; depth < 4 && current instanceof Error; depth += 1) {
    const code = (current as { code?: unknown }).code;
    if (typeof code === "string") return code;
    current = current.cause;
  }
  return undefined;
}

function isTimeout(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error.name === "TimeoutError" || error.name === "AbortError" || errorCode(error) === "UND_ERR_HEADERS_TIMEOUT")
  );
}

export class HttpClient {
  readonly baseUrl: string;
  private readonly headers: Record<string, string>;
  private readonly timeoutSeconds: number;
  private readonly fetcher: FetchLike;

  constructor(options: {
    baseUrl?: string;
    headers?: Record<string, string>;
    timeoutSeconds: number;
    fetch?: FetchLike;
  }) {
    this.baseUrl = options.baseUrl ?? "";
    this.headers = { "user-agent": "meldgrove-iam", ...lowerKeys(options.headers ?? {}) };
    this.timeoutSeconds = options.timeoutSeconds;
    this.fetcher = options.fetch ?? ((request) => fetch(request));
  }

  /** The absolute URL of `url`, which may be relative to the base URL. */
  url(url: string, params?: RequestOptions["params"]): URL {
    const absolute = /^https?:\/\//i.test(url)
      ? new URL(url)
      : new URL(this.baseUrl.replace(/\/+$/, "") + (url.startsWith("/") || url === "" ? url : `/${url}`));
    for (const [key, value] of Object.entries(params ?? {})) {
      if (value === undefined) continue;
      if (Array.isArray(value)) for (const item of value) absolute.searchParams.append(key, String(item));
      else absolute.searchParams.set(key, String(value));
    }
    return absolute;
  }

  get(url: string, options: RequestOptions = {}) {
    return this.request("GET", url, options);
  }

  post(url: string, options: RequestOptions = {}) {
    return this.request("POST", url, options);
  }

  async request(method: string, url: string, options: RequestOptions = {}): Promise<HttpResponse> {
    const target = this.url(url, options.params);
    const headers: Record<string, string> = { ...this.headers, ...lowerKeys(options.headers ?? {}) };
    let body: string | undefined;
    if (options.json !== undefined) {
      body = JSON.stringify(options.json);
      headers["content-type"] ??= "application/json";
    } else if (options.form !== undefined) {
      body = new URLSearchParams(options.form).toString();
      headers["content-type"] ??= "application/x-www-form-urlencoded";
    } else if (options.body !== undefined) {
      body = options.body;
    }
    const pauses = method === "GET" || method === "HEAD" ? [...RETRY_PAUSES] : [];
    let budget = MAX_RETRY_WAIT_SECONDS;
    for (;;) {
      let wait: number;
      try {
        const response = await this.send(method, target, headers, body, options.timeout);
        noteProgress();
        if (pauses.length === 0 || !RETRY_STATUSES.has(response.status)) return response;
        const asked = retryAfter(response.headers);
        wait = pauses.shift()!;
        if (asked !== null) wait = asked;
        if (wait > budget) return response;
      } catch (error) {
        if (!(error instanceof HttpTransportError) || !error.connect || pauses.length === 0) throw error;
        wait = pauses.shift()!;
      }
      budget -= wait;
      await timing.pause(wait);
    }
  }

  private async send(
    method: string,
    url: URL,
    headers: Record<string, string>,
    body: string | undefined,
    timeout?: number,
  ): Promise<HttpResponse> {
    const request = new Request(url, {
      method,
      headers,
      body,
      redirect: "manual",
      signal: AbortSignal.timeout((timeout ?? this.timeoutSeconds) * 1000),
    });
    let response: Response;
    try {
      response = await this.fetcher(request);
    } catch (error) {
      if (error instanceof HttpTimeoutError || error instanceof HttpTransportError) throw error;
      if (isTimeout(error)) throw new HttpTimeoutError();
      const code = errorCode(error);
      throw new HttpTransportError(`request failed${code ? ` (${code})` : ""}`, !code || CONNECT_CODES.has(code));
    }
    let text: string;
    try {
      text = await response.text();
    } catch (error) {
      if (isTimeout(error)) throw new HttpTimeoutError();
      throw new HttpTransportError("the answer broke off", false);
    }
    return new HttpResponse(response.status, response.headers, text, url.toString());
  }
}

function lowerKeys(headers: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), value]));
}
