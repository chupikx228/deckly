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

export const toApiError = (status: number, body: unknown): ApiError => {
  const parsed = problemSchema.safeParse(body);

  if (parsed.success) {
    return new ApiError(parsed.data);
  }

  return new ApiError({
    type: 'about:blank',
    title: 'Unexpected error',
    status,
    code: status >= 500 ? ERROR_CODE.INTERNAL_ERROR : ERROR_CODE.VALIDATION_FAILED,
  });
};

export const isRetryableError = (error: unknown): boolean => {
  if (isNetworkError(error)) {
    return true;
  }

  return isApiError(error) && error.status >= 500;
};
