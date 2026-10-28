import {
  ERROR_CODE,
  JOB_STAGE,
  JOB_STATUS,
  type ErrorCode,
  type GenerationJob,
  type Problem,
} from '@deckly/api-contract';
import { describe, expect, it } from 'vitest';

import type { ErrorDescriptor } from '@/shared/api';
import { GENERATION } from '@/shared/config';
import enErrors from '@/shared/i18n/locales/en/errors.json';
import ruErrors from '@/shared/i18n/locales/ru/errors.json';

import {
  ERROR_CODE_MESSAGE_KEY,
  getElapsedMs,
  getJobErrorMessageKey,
  getRequestErrorMessageKey,
  getRequestErrorTitleKey,
  isFailureWorthReporting,
  isGenerationTimedOut,
} from './generation';

const START = Date.UTC(2026, 7, 14, 10, 0, 0);

const makeJob = (elapsedMs: number): GenerationJob => ({
  jobId: '3f7c1f7e-5a4d-4a4b-9b3f-3d1c2e5a7b9d',
  status: JOB_STATUS.RUNNING,
  stage: JOB_STAGE.GENERATING_CARDS,
  progress: 0.5,
  createdAt: new Date(START).toISOString(),
  updatedAt: new Date(START + elapsedMs).toISOString(),
  result: null,
  error: null,
});

describe('getElapsedMs', () => {
  it('measures the span between the server timestamps', () => {
    expect(getElapsedMs(makeJob(45_000))).toBe(45_000);
  });

  it('is zero for a job that has not progressed', () => {
    expect(getElapsedMs(makeJob(0))).toBe(0);
  });
});

describe('isGenerationTimedOut', () => {
  it('accepts a job still inside the budget', () => {
    expect(isGenerationTimedOut(makeJob(GENERATION.TIMEOUT_MS - 1_000))).toBe(false);
  });

  it('flags a job that overran the budget', () => {
    expect(isGenerationTimedOut(makeJob(GENERATION.TIMEOUT_MS + 1_000))).toBe(true);
  });

  it('uses server time rather than the device clock', () => {
    const job = makeJob(GENERATION.TIMEOUT_MS + 1_000);

    expect(isGenerationTimedOut({ ...job, createdAt: job.updatedAt })).toBe(false);
  });
});

const makeDescriptor = (overrides: Partial<ErrorDescriptor> = {}): ErrorDescriptor => ({
  code: null,
  isNetwork: false,
  isRetryable: false,
  ...overrides,
});

const makeProblem = (code: ErrorCode): Problem => ({
  type: 'about:blank',
  title: 'Generation failed',
  status: 500,
  code,
});

const resolveKey = (bundle: Record<string, unknown>, key: string): unknown =>
  key.split('.').reduce<unknown>((value, segment) => {
    if (typeof value !== 'object' || value === null) {
      return undefined;
    }

    return Object.getOwnPropertyDescriptor(value, segment)?.value;
  }, bundle);

describe('getJobErrorMessageKey', () => {
  it('uses the code reported by the job', () => {
    expect(getJobErrorMessageKey(makeProblem(ERROR_CODE.TOPIC_REJECTED))).toBe(
      'code.TOPIC_REJECTED',
    );
  });

  it('falls back to the generic failure when the job carries no problem', () => {
    expect(getJobErrorMessageKey(null)).toBe('code.GENERATION_FAILED');
  });

  it('maps every error code to its own message', () => {
    const keys = Object.values(ERROR_CODE).map((code) => getJobErrorMessageKey(makeProblem(code)));

    expect(new Set(keys).size).toBe(Object.values(ERROR_CODE).length);
  });
});

describe('getRequestErrorMessageKey', () => {
  it('reports a lost connection before anything else', () => {
    expect(
      getRequestErrorMessageKey(
        makeDescriptor({ code: ERROR_CODE.INTERNAL_ERROR, isNetwork: true }),
      ),
    ).toBe('network.description');
  });

  it('reports a deleted job as no longer available', () => {
    expect(getRequestErrorMessageKey(makeDescriptor({ code: ERROR_CODE.JOB_NOT_FOUND }))).toBe(
      'code.JOB_NOT_FOUND',
    );
  });

  it('falls back to the generic message for an unrecognised failure', () => {
    expect(getRequestErrorMessageKey(makeDescriptor())).toBe('generic.description');
  });
});

describe('getRequestErrorTitleKey', () => {
  it('titles a deleted job as unavailable rather than broken', () => {
    expect(getRequestErrorTitleKey(makeDescriptor({ code: ERROR_CODE.JOB_NOT_FOUND }))).toBe(
      'generation.unavailableTitle',
    );
  });

  it('prefers the missing-job title over the connection title', () => {
    expect(
      getRequestErrorTitleKey(makeDescriptor({ code: ERROR_CODE.JOB_NOT_FOUND, isNetwork: true })),
    ).toBe('generation.unavailableTitle');
  });

  it('titles a transport failure as a connection problem', () => {
    expect(getRequestErrorTitleKey(makeDescriptor({ isNetwork: true }))).toBe('network.title');
  });

  it('falls back to the generic title', () => {
    expect(getRequestErrorTitleKey(makeDescriptor({ code: ERROR_CODE.RATE_LIMITED }))).toBe(
      'generic.title',
    );
  });
});

describe('isFailureWorthReporting', () => {
  const missing = makeDescriptor({ code: ERROR_CODE.JOB_NOT_FOUND });
  const transient = makeDescriptor({ isNetwork: true, isRetryable: true });

  it('reports a failure that arrived before any job data', () => {
    expect(isFailureWorthReporting(missing, undefined)).toBe(true);
  });

  it('reports a deleted job while the generation is still running', () => {
    expect(isFailureWorthReporting(missing, makeJob(1_000))).toBe(true);
  });

  it('keeps showing progress while a retryable failure is being retried', () => {
    expect(isFailureWorthReporting(transient, makeJob(1_000))).toBe(false);
  });

  it('never discards a finished job because a later poll failed', () => {
    const finished: GenerationJob = { ...makeJob(1_000), status: JOB_STATUS.SUCCEEDED };

    expect(isFailureWorthReporting(missing, finished)).toBe(false);
  });

  it('never discards a cancelled job because a later poll failed', () => {
    const cancelled: GenerationJob = { ...makeJob(1_000), status: JOB_STATUS.CANCELLED };

    expect(isFailureWorthReporting(missing, cancelled)).toBe(false);
  });
});

describe('ERROR_CODE_MESSAGE_KEY', () => {
  it.each([
    ['en', enErrors],
    ['ru', ruErrors],
  ])('resolves every code against the %s bundle', (_locale, bundle) => {
    for (const key of Object.values(ERROR_CODE_MESSAGE_KEY)) {
      expect(typeof resolveKey(bundle, key)).toBe('string');
    }
  });
});
