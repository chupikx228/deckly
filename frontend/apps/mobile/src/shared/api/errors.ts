import { ERROR_CODE, problemSchema, type ErrorCode, type Problem } from '@deckly/api-contract';

export class ApiError extends Error {
  readonly code: ErrorCode;
  readonly status: number;
  readonly retryAfterSeconds: number | null;

  constructor(problem: Problem) {
    super(problem.title);

    this.name = 'ApiError';
    this.code = problem.code;
    this.status = problem.status;
    this.retryAfterSeconds = problem.retryAfterSeconds ?? null;
  }
}

export class NetworkError extends Error {
  constructor(cause: unknown) {
    super('Network request failed');

    this.name = 'NetworkError';
    this.cause = cause;
  }
}

export const isApiError = (error: unknown): error is ApiError => error instanceof ApiError;

export const isNetworkError = (error: unknown): error is NetworkError =>
  error instanceof NetworkError;

const FALLBACK_CODE_BY_STATUS: Readonly<Record<number, ErrorCode>> = {
  400: ERROR_CODE.VALIDATION_FAILED,
  404: ERROR_CODE.JOB_NOT_FOUND,
  409: ERROR_CODE.JOB_ALREADY_TERMINAL,
  422: ERROR_CODE.VALIDATION_FAILED,
  429: ERROR_CODE.RATE_LIMITED,
  503: ERROR_CODE.UPSTREAM_UNAVAILABLE,
};

const toFallbackCode = (status: number): ErrorCode =>
  FALLBACK_CODE_BY_STATUS[status] ??
  (status >= 500 ? ERROR_CODE.INTERNAL_ERROR : ERROR_CODE.VALIDATION_FAILED);

export const toApiError = (status: number, body: unknown): ApiError => {
  const parsed = problemSchema.safeParse(body);

  if (parsed.success) {
    return new ApiError(parsed.data);
  }

  return new ApiError({
    type: 'about:blank',
    title: 'Unexpected error',
    status,
    code: toFallbackCode(status),
  });
};

export const isRetryableError = (error: unknown): boolean => {
  if (isNetworkError(error)) {
    return true;
  }

  if (!isApiError(error)) {
    return false;
  }

  return error.status >= 500 || error.code === ERROR_CODE.RATE_LIMITED;
};

export interface ErrorDescriptor {
  code: ErrorCode | null;
  isNetwork: boolean;
  isRetryable: boolean;
}

export const describeError = (error: unknown): ErrorDescriptor => ({
  code: isApiError(error) ? error.code : null,
  isNetwork: isNetworkError(error),
  isRetryable: isRetryableError(error),
});
