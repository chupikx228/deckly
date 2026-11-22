import { ERROR_CODE, type ErrorCode } from '@deckly/api-contract';

import { ApiError } from '../errors';

const problem = (title: string, status: number, code: ErrorCode): ApiError =>
  new ApiError({ type: 'about:blank', title, status, code });

export const notFound = (): ApiError => problem('Job not found', 404, ERROR_CODE.JOB_NOT_FOUND);

export const documentTooLarge = (): ApiError =>
  problem('Document too large', 413, ERROR_CODE.DOCUMENT_TOO_LARGE);

export const unsupportedArticleUrl = (): ApiError =>
  problem('Unsupported article URL', 422, ERROR_CODE.UNSUPPORTED_ARTICLE_URL);

export const unsupportedDocumentType = (): ApiError =>
  problem('Unsupported document type', 415, ERROR_CODE.UNSUPPORTED_DOCUMENT_TYPE);

export const unsupportedRecordingUrl = (): ApiError =>
  problem('Unsupported recording URL', 422, ERROR_CODE.UNSUPPORTED_RECORDING_URL);

export const recordingTooLong = (): ApiError =>
  problem('Recording too long', 422, ERROR_CODE.RECORDING_TOO_LONG);

export const recordingUnavailable = (): ApiError =>
  problem('Recording unavailable', 422, ERROR_CODE.RECORDING_UNAVAILABLE);

export const validationFailed = (title: string): ApiError =>
  problem(title, 400, ERROR_CODE.VALIDATION_FAILED);

export const documentRegenerationUnsupported = (): ApiError =>
  problem(
    'A document-sourced note cannot be regenerated',
    422,
    ERROR_CODE.DOCUMENT_REGENERATION_UNSUPPORTED,
  );
