import {
  ERROR_CODE,
  JOB_STAGE,
  isTerminalJobStatus,
  type ErrorCode,
  type GenerationJob,
  type JobStage,
  type Problem,
} from '@deckly/api-contract';

import type { ErrorDescriptor } from '@/shared/api';
import { GENERATION } from '@/shared/config';

export const STAGE_LABEL_KEY = {
  [JOB_STAGE.PLANNING]: 'stage.planning',
  [JOB_STAGE.RETRIEVING_SOURCES]: 'stage.retrieving_sources',
  [JOB_STAGE.PARSING_SOURCES]: 'stage.parsing_sources',
  [JOB_STAGE.GENERATING_CARDS]: 'stage.generating_cards',
  [JOB_STAGE.FETCHING_MEDIA]: 'stage.fetching_media',
  [JOB_STAGE.FINALIZING]: 'stage.finalizing',
} as const satisfies Record<JobStage, string>;

export const ERROR_CODE_MESSAGE_KEY = {
  [ERROR_CODE.VALIDATION_FAILED]: 'code.VALIDATION_FAILED',
  [ERROR_CODE.TOPIC_REJECTED]: 'code.TOPIC_REJECTED',
  [ERROR_CODE.RATE_LIMITED]: 'code.RATE_LIMITED',
  [ERROR_CODE.JOB_NOT_FOUND]: 'code.JOB_NOT_FOUND',
  [ERROR_CODE.JOB_ALREADY_TERMINAL]: 'code.JOB_ALREADY_TERMINAL',
  [ERROR_CODE.IDEMPOTENCY_KEY_CONFLICT]: 'code.IDEMPOTENCY_KEY_CONFLICT',
  [ERROR_CODE.PROVIDER_UNAVAILABLE]: 'code.PROVIDER_UNAVAILABLE',
  [ERROR_CODE.NO_VALID_CONTENT]: 'code.NO_VALID_CONTENT',
  [ERROR_CODE.GENERATION_FAILED]: 'code.GENERATION_FAILED',
  [ERROR_CODE.DOCUMENT_UNREADABLE]: 'code.DOCUMENT_UNREADABLE',
  [ERROR_CODE.DOCUMENT_ENCRYPTED]: 'code.DOCUMENT_ENCRYPTED',
  [ERROR_CODE.DOCUMENT_HAS_NO_TEXT]: 'code.DOCUMENT_HAS_NO_TEXT',
  [ERROR_CODE.DOCUMENT_REJECTED]: 'code.DOCUMENT_REJECTED',
  [ERROR_CODE.DOCUMENT_REGENERATION_UNSUPPORTED]: 'code.DOCUMENT_REGENERATION_UNSUPPORTED',
  [ERROR_CODE.DOCUMENT_NOT_SUPPORTED_FOR_GENERATION]: 'code.DOCUMENT_NOT_SUPPORTED_FOR_GENERATION',
  [ERROR_CODE.UPSTREAM_UNAVAILABLE]: 'code.UPSTREAM_UNAVAILABLE',
  [ERROR_CODE.DOCUMENT_NOT_FOUND]: 'code.DOCUMENT_NOT_FOUND',
  [ERROR_CODE.DOCUMENT_TOO_LARGE]: 'code.DOCUMENT_TOO_LARGE',
  [ERROR_CODE.UNSUPPORTED_DOCUMENT_TYPE]: 'code.UNSUPPORTED_DOCUMENT_TYPE',
  [ERROR_CODE.RECORDING_TOO_LONG]: 'code.RECORDING_TOO_LONG',
  [ERROR_CODE.UNSUPPORTED_RECORDING_URL]: 'code.UNSUPPORTED_RECORDING_URL',
  [ERROR_CODE.RECORDING_UNAVAILABLE]: 'code.RECORDING_UNAVAILABLE',
  [ERROR_CODE.UNSUPPORTED_ARTICLE_URL]: 'code.UNSUPPORTED_ARTICLE_URL',
  [ERROR_CODE.ARTICLE_UNAVAILABLE]: 'code.ARTICLE_UNAVAILABLE',
  [ERROR_CODE.ARTICLE_BLOCKED_BY_ROBOTS]: 'code.ARTICLE_BLOCKED_BY_ROBOTS',
  [ERROR_CODE.ARTICLE_HAS_NO_TEXT]: 'code.ARTICLE_HAS_NO_TEXT',
  [ERROR_CODE.ROUTE_NOT_FOUND]: 'code.ROUTE_NOT_FOUND',
  [ERROR_CODE.METHOD_NOT_ALLOWED]: 'code.METHOD_NOT_ALLOWED',
  [ERROR_CODE.INTERNAL_ERROR]: 'code.INTERNAL_ERROR',
} as const satisfies Record<ErrorCode, string>;

const FALLBACK_MESSAGE_KEY = {
  GENERIC: 'generic.description',
  NETWORK: 'network.description',
} as const;

const ERROR_TITLE_KEY = {
  UNAVAILABLE: 'generation.unavailableTitle',
  NETWORK: 'network.title',
  GENERIC: 'generic.title',
} as const;

export type ErrorMessageKey =
  | (typeof ERROR_CODE_MESSAGE_KEY)[ErrorCode]
  | (typeof FALLBACK_MESSAGE_KEY)[keyof typeof FALLBACK_MESSAGE_KEY];

export type ErrorTitleKey = (typeof ERROR_TITLE_KEY)[keyof typeof ERROR_TITLE_KEY];

export const getElapsedMs = (job: GenerationJob): number =>
  Date.parse(job.updatedAt) - Date.parse(job.createdAt);

export const isGenerationTimedOut = (job: GenerationJob): boolean =>
  getElapsedMs(job) > GENERATION.TIMEOUT_MS;

export const getJobErrorMessageKey = (error: Problem | null): ErrorMessageKey =>
  error === null
    ? ERROR_CODE_MESSAGE_KEY[ERROR_CODE.GENERATION_FAILED]
    : ERROR_CODE_MESSAGE_KEY[error.code];

export const getRequestErrorMessageKey = (error: ErrorDescriptor): ErrorMessageKey => {
  if (error.isNetwork) {
    return FALLBACK_MESSAGE_KEY.NETWORK;
  }

  if (error.code !== null) {
    return ERROR_CODE_MESSAGE_KEY[error.code];
  }

  return FALLBACK_MESSAGE_KEY.GENERIC;
};

export const isFailureWorthReporting = (
  failure: ErrorDescriptor,
  job: GenerationJob | undefined,
): boolean => {
  if (isTerminalJobStatus(job?.status)) {
    return false;
  }

  return job === undefined || !failure.isRetryable;
};

export const getRequestErrorTitleKey = (error: ErrorDescriptor): ErrorTitleKey => {
  if (error.code === ERROR_CODE.JOB_NOT_FOUND) {
    return ERROR_TITLE_KEY.UNAVAILABLE;
  }

  if (error.isNetwork) {
    return ERROR_TITLE_KEY.NETWORK;
  }

  return ERROR_TITLE_KEY.GENERIC;
};
