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
  [ERROR_CODE.GENERATION_FAILED]: 'code.GENERATION_FAILED',
  [ERROR_CODE.UPSTREAM_UNAVAILABLE]: 'code.UPSTREAM_UNAVAILABLE',
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
