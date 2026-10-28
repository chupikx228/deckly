import { ERROR_CODE, type Problem } from '@deckly/api-contract';
import { describe, expect, it } from 'vitest';

import { ApiError, NetworkError, describeError, isRetryableError, toApiError } from './errors';

const makeProblem = (overrides: Partial<Problem> = {}): Problem => ({
  type: 'about:blank',
  title: 'Job not found',
  status: 404,
  code: ERROR_CODE.JOB_NOT_FOUND,
  ...overrides,
});

describe('toApiError', () => {
  it('prefers a well-formed problem body over the status fallback', () => {
    const error = toApiError(404, makeProblem({ code: ERROR_CODE.TOPIC_REJECTED }));

    expect(error.code).toBe(ERROR_CODE.TOPIC_REJECTED);
  });

  it('reads a 404 with an unparseable body as a missing job', () => {
    expect(toApiError(404, null).code).toBe(ERROR_CODE.JOB_NOT_FOUND);
  });

  it('reads a 404 served as HTML by a proxy as a missing job', () => {
    expect(toApiError(404, '<html><body>Not Found</body></html>').code).toBe(
      ERROR_CODE.JOB_NOT_FOUND,
    );
  });

  it('reads a bodyless 409 as an already finished job', () => {
    expect(toApiError(409, null).code).toBe(ERROR_CODE.JOB_ALREADY_TERMINAL);
  });

  it('reads a bodyless 429 as rate limiting', () => {
    expect(toApiError(429, null).code).toBe(ERROR_CODE.RATE_LIMITED);
  });

  it('reads a bodyless 503 as an unavailable upstream rather than an internal error', () => {
    expect(toApiError(503, null).code).toBe(ERROR_CODE.UPSTREAM_UNAVAILABLE);
  });

  it('falls back to an internal error for an unmapped server status', () => {
    expect(toApiError(500, null).code).toBe(ERROR_CODE.INTERNAL_ERROR);
  });

  it('falls back to a validation failure for an unmapped client status', () => {
    expect(toApiError(418, null).code).toBe(ERROR_CODE.VALIDATION_FAILED);
  });

  it('keeps a partial problem body from being read as valid', () => {
    expect(toApiError(404, { title: 'Not found' }).code).toBe(ERROR_CODE.JOB_NOT_FOUND);
  });
});

describe('isRetryableError', () => {
  it('retries a transport failure', () => {
    expect(isRetryableError(new NetworkError(new Error('socket closed')))).toBe(true);
  });

  it('retries rate limiting so polling survives a throttled window', () => {
    expect(
      isRetryableError(new ApiError(makeProblem({ status: 429, code: ERROR_CODE.RATE_LIMITED }))),
    ).toBe(true);
  });

  it('does not retry a missing job', () => {
    expect(isRetryableError(new ApiError(makeProblem()))).toBe(false);
  });

  it('retries a server error', () => {
    expect(
      isRetryableError(
        new ApiError(makeProblem({ status: 502, code: ERROR_CODE.UPSTREAM_UNAVAILABLE })),
      ),
    ).toBe(true);
  });

  it('does not retry an error it cannot classify', () => {
    expect(isRetryableError(new Error('boom'))).toBe(false);
  });
});

describe('describeError', () => {
  it('describes an api error by its code', () => {
    expect(describeError(new ApiError(makeProblem()))).toStrictEqual({
      code: ERROR_CODE.JOB_NOT_FOUND,
      isNetwork: false,
      isRetryable: false,
    });
  });

  it('describes a transport failure without a code', () => {
    expect(describeError(new NetworkError(new Error('offline')))).toStrictEqual({
      code: null,
      isNetwork: true,
      isRetryable: true,
    });
  });

  it('describes an unclassified throw as neither', () => {
    expect(describeError('something odd')).toStrictEqual({
      code: null,
      isNetwork: false,
      isRetryable: false,
    });
  });
});
