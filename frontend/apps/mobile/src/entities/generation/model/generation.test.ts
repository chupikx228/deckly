import { JOB_STAGE, JOB_STATUS, type GenerationJob } from '@deckly/api-contract';
import { describe, expect, it } from 'vitest';

import { GENERATION } from '@/shared/config';

import { getElapsedMs, isGenerationTimedOut } from './generation';

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
