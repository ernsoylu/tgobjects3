/**
 * CSRF-on-mutations invariant for the reworked Admin API client (task 4.3).
 *
 * These tests exercise the request/response interceptors of `createAdminClient`
 * directly, with a fake axios adapter that both records the headers the client
 * actually puts on the wire and scripts the backend's reply. Asserting on the
 * attached headers (rather than a live server) keeps the test hermetic while
 * still covering the real interceptor logic in `client.ts`.
 *
 * Covers **Property 3** (console half): a state-mutating request issued before
 * the CSRF token is bootstrapped — or carrying a token that does not match the
 * one the backend expects — is rejected (the backend answers 403) and applies
 * no change, whereas after a successful bootstrap the same request carries the
 * cached token and succeeds. Also covers the token-caching and 401 → session-end
 * hooks the auth layer relies on.
 *
 * Validates: Requirements 1.5, 11.5, 11.6.
 */
import type { AxiosAdapter, AxiosHeaderValue, AxiosResponse } from "axios";
import { AxiosHeaders } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CSRF_HEADER,
  clearCredentials,
  createAdminClient,
  setCredentials,
  setUnauthorizedHandler,
} from "./client";

/** The token the fake backend hands back on a bootstrap read. */
const SERVER_CSRF_TOKEN = "csrf-token-abc123";

/** A record of one request the client attempted, as the adapter saw it. */
interface CapturedRequest {
  readonly method: string;
  readonly url: string | undefined;
  readonly authorization: AxiosHeaderValue | undefined;
  readonly csrf: AxiosHeaderValue | undefined;
}

/**
 * A fake axios adapter that captures each outgoing request's headers and
 * replies with a scripted response. The `reply` callback receives the request
 * number (0-based) and the normalised headers so a test can model a backend
 * that only accepts the correct CSRF token.
 */
function makeFakeAdapter(
  reply: (
    index: number,
    captured: CapturedRequest,
  ) => { status: number; headers?: Record<string, string> },
): { adapter: AxiosAdapter; captured: CapturedRequest[] } {
  const captured: CapturedRequest[] = [];
  const adapter: AxiosAdapter = async (config) => {
    const headers = AxiosHeaders.from(config.headers);
    const request: CapturedRequest = {
      method: (config.method ?? "get").toLowerCase(),
      url: config.url,
      authorization: headers.get("Authorization"),
      csrf: headers.get(CSRF_HEADER),
    };
    captured.push(request);
    const scripted = reply(captured.length - 1, request);
    const response: AxiosResponse = {
      data: {},
      status: scripted.status,
      statusText: String(scripted.status),
      headers: scripted.headers ?? {},
      config,
    };
    if (scripted.status >= 200 && scripted.status < 300) {
      return response;
    }
    // Mirror axios's own rejection shape for non-2xx responses so the response
    // interceptor's error branch (401 handling) runs exactly as in production.
    const error = Object.assign(new Error(`Request failed with status ${scripted.status}`), {
      isAxiosError: true,
      config,
      response,
      toJSON: () => ({}),
    });
    return Promise.reject(error);
  };
  return { adapter, captured };
}

afterEach(() => {
  clearCredentials();
  setUnauthorizedHandler(null);
});

describe("Admin API client CSRF handling (Property 3)", () => {
  it("caches the bootstrap token and attaches it only on unsafe methods", async () => {
    const { adapter, captured } = makeFakeAdapter((index) =>
      index === 0
        ? { status: 200, headers: { [CSRF_HEADER.toLowerCase()]: SERVER_CSRF_TOKEN } }
        : { status: 200 },
    );
    const client = createAdminClient();
    client.defaults.adapter = adapter;

    setCredentials("hunter2");

    // Bootstrap GET: no CSRF attached (safe method), server echoes the token.
    await client.get("/buckets");
    // Subsequent unsafe methods must now carry the cached token...
    await client.post("/setup/finalize", {});
    await client.put("/settings", {});
    await client.delete("/keys/key-1");
    // ...while safe methods never carry it.
    await client.head("/buckets");
    await client.get("/jobs");

    const [bootstrap, post, put, del, head, get] = captured;

    // Every request carries Authorization once credentials are set.
    for (const req of captured) {
      expect(req.authorization).toMatch(/^Basic /);
    }

    expect(bootstrap?.csrf).toBeUndefined();
    expect(post?.csrf).toBe(SERVER_CSRF_TOKEN);
    expect(put?.csrf).toBe(SERVER_CSRF_TOKEN);
    expect(del?.csrf).toBe(SERVER_CSRF_TOKEN);
    expect(head?.csrf).toBeUndefined();
    expect(get?.csrf).toBeUndefined();
  });

  it("sends no CSRF token on a mutation before bootstrap and persists no change on that attempt", async () => {
    // Property 3 (console half): a mutation issued before the token is
    // bootstrapped carries no `X-CSRF-Token` header, so the backend rejects
    // that attempt (403) and persists nothing on it. The client additionally
    // bootstraps-and-retries once (task 12.1); here the backend never accepts
    // the mutation (it 403s regardless of token), so the retry also fails and
    // the mutation is never accepted here, so the first attempt — and the
    // single retry — both 403, proving the enforcement holds through the retry.
    const firstMutation: { csrf: AxiosHeaderValue | undefined } = { csrf: "unset" };
    const { adapter, captured } = makeFakeAdapter((index, req) => {
      const isUnsafe = !["get", "head", "options"].includes(req.method);
      if (isUnsafe && index === 0) {
        firstMutation.csrf = req.csrf;
      }
      if (req.method === "get") {
        return { status: 200, headers: { [CSRF_HEADER.toLowerCase()]: SERVER_CSRF_TOKEN } };
      }
      // Mutation never accepted in this scenario.
      return { status: 403 };
    });
    const client = createAdminClient();
    client.defaults.adapter = adapter;

    setCredentials("hunter2");

    await expect(client.post("/settings", {})).rejects.toMatchObject({
      response: { status: 403 },
    });
    // The first mutation attempt carried no token (pre-bootstrap); no mutation
    // request was ever accepted (every "post" reply was 403).
    expect(firstMutation.csrf).toBeUndefined();
    const acceptedMutations = captured.filter(
      (req) => !["get", "head", "options"].includes(req.method) && req.csrf === SERVER_CSRF_TOKEN,
    );
    // The retried mutation did carry the bootstrapped token, but the backend
    // still rejected it — enforcement is the backend's call, the client only
    // offers one refresh+retry.
    expect(acceptedMutations.length).toBeLessThanOrEqual(1);
  });

  it("accepts a mutation once the token is bootstrapped and carried", async () => {
    // The complement of the enforcement test: after a bootstrap GET caches the
    // token, the mutation carries it and is accepted exactly once.
    let applied = 0;
    const { adapter } = makeFakeAdapter((_index, req) => {
      const isUnsafe = !["get", "head", "options"].includes(req.method);
      if (req.method === "get") {
        return { status: 200, headers: { [CSRF_HEADER.toLowerCase()]: SERVER_CSRF_TOKEN } };
      }
      if (isUnsafe && req.csrf !== SERVER_CSRF_TOKEN) {
        return { status: 403 };
      }
      if (isUnsafe) {
        applied += 1;
      }
      return { status: 200 };
    });
    const client = createAdminClient();
    client.defaults.adapter = adapter;

    setCredentials("hunter2");

    await client.get("/buckets");
    await expect(client.post("/settings", {})).resolves.toMatchObject({ status: 200 });
    expect(applied).toBe(1);
  });

  it("retries once then rejects when a stale token cannot be refreshed to a usable one", async () => {
    // The backend rotated its expected token and the client holds a stale one.
    // The 403 triggers a bootstrap refresh; here the backend's bootstrap GET
    // keeps echoing a token the mutation path still won't accept, so the single
    // retry also 403s and no change is applied — the mismatch stays rejected.
    const STALE_TOKEN = "csrf-old";
    let mutationAttempts = 0;
    const { adapter } = makeFakeAdapter((_index, req) => {
      const isUnsafe = !["get", "head", "options"].includes(req.method);
      if (req.method === "get") {
        return { status: 200, headers: { [CSRF_HEADER.toLowerCase()]: STALE_TOKEN } };
      }
      // The backend expects a token the client can never obtain here, so every
      // mutation attempt is rejected and nothing is applied.
      if (isUnsafe) {
        mutationAttempts += 1;
        return { status: 403 };
      }
      return { status: 200 };
    });
    const client = createAdminClient();
    client.defaults.adapter = adapter;

    setCredentials("hunter2");

    await client.get("/buckets");

    await expect(client.post("/settings", {})).rejects.toMatchObject({
      response: { status: 403 },
    });
    // Original attempt + exactly one retry: no change ever applied.
    expect(mutationAttempts).toBe(2);
  });

  it("invokes the registered unauthorized handler on a 401 response", async () => {
    const { adapter } = makeFakeAdapter(() => ({ status: 401 }));
    const client = createAdminClient();
    client.defaults.adapter = adapter;

    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    setCredentials("hunter2");

    await expect(client.get("/buckets")).rejects.toMatchObject({
      response: { status: 401 },
    });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("does not invoke the unauthorized handler on a 403 response", async () => {
    // A 403 on a *safe* method is never a CSRF-retry case, so it rejects
    // straight through without touching the unauthorized hook.
    const { adapter } = makeFakeAdapter(() => ({ status: 403 }));
    const client = createAdminClient();
    client.defaults.adapter = adapter;

    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    setCredentials("hunter2");

    await expect(client.get("/buckets")).rejects.toMatchObject({
      response: { status: 403 },
    });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});

describe("Admin API client 403-CSRF bootstrap-and-retry (task 12.1, Req 11.6)", () => {
  it("refreshes the token via bootstrap and retries the mutation once on success", async () => {
    // The backend starts with a stale token the client does not yet hold; the
    // first mutation 403s. The interceptor must then re-run the bootstrap GET
    // (which echoes the current token) and retry the mutation, which now
    // carries the fresh token and succeeds.
    const FRESH_TOKEN = "csrf-fresh";
    let applied = 0;
    const { adapter, captured } = makeFakeAdapter((_index, req) => {
      if (req.method === "get") {
        // Bootstrap read always echoes the current (fresh) token.
        return { status: 200, headers: { [CSRF_HEADER.toLowerCase()]: FRESH_TOKEN } };
      }
      // Mutation: accepted only with the fresh token, else 403.
      if (req.csrf !== FRESH_TOKEN) {
        return { status: 403 };
      }
      applied += 1;
      return { status: 200 };
    });
    const client = createAdminClient();
    client.defaults.adapter = adapter;

    setCredentials("hunter2");

    // No bootstrap yet: the POST has no token, 403s, triggers a bootstrap GET,
    // then the retried POST carries FRESH_TOKEN and succeeds.
    await expect(client.post("/settings", {})).resolves.toMatchObject({ status: 200 });

    const methods = captured.map((req) => req.method);
    // POST (403) → GET (bootstrap) → POST (retry, 200).
    expect(methods).toEqual(["post", "get", "post"]);
    expect(captured[0]?.csrf).toBeUndefined();
    expect(captured[2]?.csrf).toBe(FRESH_TOKEN);
    expect(applied).toBe(1);
  });

  it("surfaces the 403 after a single retry still fails (no loop)", async () => {
    // The backend 403s every mutation regardless of token (e.g. the refresh
    // never yields a usable token). The client must retry exactly once and then
    // surface the 403 rather than looping forever.
    let bootstrapCount = 0;
    let mutationCount = 0;
    const { adapter } = makeFakeAdapter((_index, req) => {
      if (req.method === "get") {
        bootstrapCount += 1;
        return { status: 200, headers: { [CSRF_HEADER.toLowerCase()]: "whatever" } };
      }
      mutationCount += 1;
      return { status: 403 };
    });
    const client = createAdminClient();
    client.defaults.adapter = adapter;

    setCredentials("hunter2");

    await expect(client.post("/settings", {})).rejects.toMatchObject({
      response: { status: 403 },
    });
    // Original + exactly one retry = two mutation attempts; one bootstrap.
    expect(mutationCount).toBe(2);
    expect(bootstrapCount).toBe(1);
  });

  it("surfaces the original 403 when the bootstrap refresh itself fails", async () => {
    // If the token-refresh GET fails (e.g. 503), there is nothing to retry
    // with: the caller sees the original 403, not the bootstrap failure.
    let mutationCount = 0;
    const { adapter } = makeFakeAdapter((_index, req) => {
      if (req.method === "get") {
        return { status: 503 };
      }
      mutationCount += 1;
      return { status: 403 };
    });
    const client = createAdminClient();
    client.defaults.adapter = adapter;

    setCredentials("hunter2");

    await expect(client.post("/settings", {})).rejects.toMatchObject({
      response: { status: 403 },
    });
    // Only the original mutation ran; no retry after the refresh failed.
    expect(mutationCount).toBe(1);
  });

  it("does not retry a 403 on a safe method", async () => {
    let getCount = 0;
    const { adapter } = makeFakeAdapter((_index, req) => {
      if (req.method === "get") {
        getCount += 1;
      }
      return { status: 403 };
    });
    const client = createAdminClient();
    client.defaults.adapter = adapter;

    setCredentials("hunter2");

    await expect(client.get("/buckets")).rejects.toMatchObject({
      response: { status: 403 },
    });
    // Exactly one GET: no bootstrap-and-retry machinery for a safe method.
    expect(getCount).toBe(1);
  });
});
