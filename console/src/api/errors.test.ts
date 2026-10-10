/**
 * Status → operator-readable message mapping (task 12.1).
 *
 * Exercises {@link describeAdminError} / {@link errorMessage} against the
 * design's "Admin API status → Dashboard behaviour" table: each HTTP status (and
 * the no-response network/timeout case) is classified and rendered as the table
 * prescribes. Axios errors are constructed with the same shape the real client
 * produces so the parsing path matches production.
 *
 * Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.6, 11.6.
 */
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { describe, expect, it } from "vitest";

import {
  CSRF_FAILED_MESSAGE,
  GATEWAY_UNREACHABLE_MESSAGE,
  UNEXPECTED_GATEWAY_ERROR_MESSAGE,
  describeAdminError,
  errorMessage,
} from "./errors";

/** Build an AxiosError carrying a response with the given status + body. */
function axiosErrorWithResponse(status: number, data: unknown): AxiosError {
  const config = { headers: new AxiosHeaders() } as never;
  const response = {
    data,
    status,
    statusText: String(status),
    headers: {},
    config,
  } as AxiosResponse;
  const error = new AxiosError(
    `Request failed with status ${status}`,
    status >= 500 ? "ERR_BAD_RESPONSE" : "ERR_BAD_REQUEST",
    config,
    {},
    response,
  );
  return error;
}

/** Build an AxiosError with no response, modelling a network / timeout fault. */
function axiosNetworkError(code: string): AxiosError {
  const config = { headers: new AxiosHeaders() } as never;
  return new AxiosError("network down", code, config, {});
}

describe("describeAdminError — status → behaviour mapping", () => {
  it("maps a network error (no response) to the unreachable message, retryable", () => {
    const info = describeAdminError(axiosNetworkError("ERR_NETWORK"));
    expect(info.kind).toBe("network");
    expect(info.status).toBeNull();
    expect(info.message).toBe(GATEWAY_UNREACHABLE_MESSAGE);
    expect(info.retryable).toBe(true);
  });

  it("maps a 30s timeout to the unreachable message, retryable", () => {
    const info = describeAdminError(axiosNetworkError("ECONNABORTED"));
    expect(info.kind).toBe("network");
    expect(info.message).toBe(GATEWAY_UNREACHABLE_MESSAGE);
    expect(info.retryable).toBe(true);
  });

  it("maps a 401 to a terminal, non-retryable session message", () => {
    const info = describeAdminError(axiosErrorWithResponse(401, { detail: "unauthorized" }));
    expect(info.kind).toBe("unauthorized");
    expect(info.retryable).toBe(false);
  });

  it("maps a surviving 403 to the readable CSRF-failed message (Req 11.6)", () => {
    const info = describeAdminError(axiosErrorWithResponse(403, { detail: "CSRF token invalid" }));
    expect(info.kind).toBe("csrf");
    expect(info.message).toBe(CSRF_FAILED_MESSAGE);
    expect(info.retryable).toBe(false);
  });

  it("maps a 409 to its response detail, staying on screen", () => {
    const detail = "staging size cannot be reduced below 1048576 bytes reserved";
    const info = describeAdminError(axiosErrorWithResponse(409, { detail }));
    expect(info.kind).toBe("conflict");
    expect(info.message).toBe(detail);
    expect(info.retryable).toBe(false);
  });

  it("maps a 422 validation array to a field-named, range-bearing message (Req 12.6)", () => {
    const info = describeAdminError(
      axiosErrorWithResponse(422, {
        detail: [
          {
            loc: ["body", "read_ahead"],
            msg: "Input should be less than or equal to 32",
            type: "less_than_equal",
          },
        ],
      }),
    );
    expect(info.kind).toBe("validation");
    expect(info.message).toBe("read_ahead: Input should be less than or equal to 32");
    expect(info.retryable).toBe(false);
  });

  it("joins multiple 422 validation items", () => {
    const info = describeAdminError(
      axiosErrorWithResponse(422, {
        detail: [
          { loc: ["body", "staging_max_size"], msg: "Input should be greater than or equal to 1" },
          { loc: ["body", "read_ahead"], msg: "Input should be less than or equal to 32" },
        ],
      }),
    );
    expect(info.message).toBe(
      "staging_max_size: Input should be greater than or equal to 1; " +
        "read_ahead: Input should be less than or equal to 32",
    );
  });

  it("maps a 503 to its detail and marks it retryable", () => {
    const info = describeAdminError(
      axiosErrorWithResponse(503, { detail: "Telegram unavailable" }),
    );
    expect(info.kind).toBe("unavailable");
    expect(info.message).toBe("Telegram unavailable");
    expect(info.retryable).toBe(true);
  });

  it("maps a 503 with no detail to a readable unavailable message, retryable", () => {
    const info = describeAdminError(axiosErrorWithResponse(503, {}));
    expect(info.kind).toBe("unavailable");
    expect(info.retryable).toBe(true);
    expect(info.message.length).toBeGreaterThan(0);
  });

  it("maps other 5xx to the unexpected gateway error, not a success (Req 12.4)", () => {
    for (const status of [500, 502, 504]) {
      const info = describeAdminError(axiosErrorWithResponse(status, { detail: "boom" }));
      expect(info.kind).toBe("server");
      expect(info.message).toBe(UNEXPECTED_GATEWAY_ERROR_MESSAGE);
      expect(info.retryable).toBe(false);
    }
  });

  it("falls back to a readable message for a plain Error and unknown values", () => {
    expect(errorMessage(new Error("something broke"))).toBe("something broke");
    expect(errorMessage("just a string")).toBe("Unknown error.");
    expect(errorMessage(undefined)).toBe("Unknown error.");
  });

  it("errorMessage returns the same string describeAdminError carries", () => {
    const err = axiosErrorWithResponse(409, { detail: "already finalized" });
    expect(errorMessage(err)).toBe(describeAdminError(err).message);
  });
});
