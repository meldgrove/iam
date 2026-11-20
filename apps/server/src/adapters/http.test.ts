import { beforeEach, describe, expect, it } from "vitest";
import { HttpClient, HttpTimeoutError, HttpTransportError } from "./http";
import { noteProgress, secondsWithoutProgress } from "../worker/liveness";
import { answer, answers, connectError, mockFetch, recordPauses, timeoutError, type MockFetch } from "../test/mock-fetch";

/** Reads that fail for a passing reason are tried again in place. */
const URL_ = "https://app.example.invalid/users";

function client(fetch: MockFetch) {
  return new HttpClient({ timeoutSeconds: 5, fetch });
}

describe("the adapters' HTTP client", () => {
  let pauses: number[];
  beforeEach(() => {
    pauses = recordPauses().pauses;
  });

  it("tries a read again after a passing failure", async () => {
    const fetch = mockFetch(answers(answer(503), answer(502), answer(200)));
    expect((await client(fetch).get(URL_)).status).toBe(200);
    expect(fetch.requests).toHaveLength(3);
    expect(pauses).toEqual([1, 4]);
  });

  it("gives a read up after three tries", async () => {
    const fetch = mockFetch(() => answer(504));
    expect((await client(fetch).get(URL_)).status).toBe(504);
    expect(fetch.requests).toHaveLength(3);
  });

  it.each(["7", "date"])("waits as long as the server asks (%s)", async (kind) => {
    const header = kind === "date" ? new Date(Date.now() + 8000).toUTCString() : kind;
    const fetch = mockFetch(answers(answer(429, "", { "retry-after": header }), answer(200)));
    expect((await client(fetch).get(URL_)).status).toBe(200);
    expect(pauses).toHaveLength(1);
    expect(pauses[0]).toBeGreaterThanOrEqual(6);
    expect(pauses[0]).toBeLessThanOrEqual(8);
  });

  it.each(["NaN", "-5", "soon", "1.5", "²"])("gives a malformed Retry-After (%s) the usual pause", async (header) => {
    const fetch = mockFetch(answers(answer(503, "", { "retry-after": header }), answer(200)));
    expect((await client(fetch).get(URL_)).status).toBe(200);
    expect(pauses).toEqual([1]);
  });

  it("leaves a long Retry-After to the job", async () => {
    const fetch = mockFetch(() => answer(429, "", { "retry-after": "120" }));
    expect((await client(fetch).get(URL_)).status).toBe(429);
    expect(fetch.requests).toHaveLength(1);
    expect(pauses).toEqual([]);
  });

  it("adds the pauses of one request up to 30 seconds at most", async () => {
    const fetch = mockFetch(() => answer(429, "", { "retry-after": "20" }));
    expect((await client(fetch).get(URL_)).status).toBe(429);
    expect(pauses).toEqual([20]);
    expect(fetch.requests).toHaveLength(2);
  });

  it("does not try a request that timed out again", async () => {
    const fetch = mockFetch(answers(timeoutError(), answer(200)));
    await expect(client(fetch).get(URL_)).rejects.toBeInstanceOf(HttpTimeoutError);
    expect(fetch.requests).toHaveLength(1);
    expect(pauses).toEqual([]);
  });

  it("tries a refused connection again", async () => {
    expect((await client(mockFetch(answers(connectError(), answer(200)))).get(URL_)).status).toBe(200);
    const fetch = mockFetch(answers(connectError(), connectError(), connectError()));
    await expect(client(fetch).get(URL_)).rejects.toBeInstanceOf(HttpTransportError);
  });

  it.each(["POST", "PUT", "PATCH", "DELETE"])("never sends a change (%s) twice", async (method) => {
    const fetch = mockFetch(() => answer(503));
    expect((await client(fetch).request(method, URL_)).status).toBe(503);
    expect(fetch.requests).toHaveLength(1);
    expect(pauses).toEqual([]);
  });

  it("does not try other errors again", async () => {
    const fetch = mockFetch(() => answer(500));
    expect((await client(fetch).get(URL_)).status).toBe(500);
    expect(fetch.requests).toHaveLength(1);
  });

  it("counts every answer as progress for the watchdog", async () => {
    noteProgress();
    await new Promise((resolve) => setTimeout(resolve, 30));
    const before = secondsWithoutProgress();
    await client(mockFetch(() => answer(200))).get(URL_);
    expect(secondsWithoutProgress()).toBeLessThan(before);
  });

  it("does not follow redirects", async () => {
    const fetch = mockFetch(() => answer(302, "", { location: "https://elsewhere.example.invalid/" }));
    expect((await client(fetch).get(URL_)).status).toBe(302);
    expect(fetch.requests).toHaveLength(1);
  });
});
