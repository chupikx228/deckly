import { describe, expect, it } from 'vitest';

import { JOB_STAGE, JOB_STATUS, NOTE_TYPE, isTerminalJobStatus } from './enums';
import { generationJobSchema, generationRequestSchema, problemSchema } from './schemas';

const runningJob = {
  jobId: '3f7c1f7e-5a4d-4a4b-9b3f-3d1c2e5a7b9d',
  status: JOB_STATUS.RUNNING,
  stage: JOB_STAGE.GENERATING_CARDS,
  progress: 0.62,
  createdAt: '2026-08-14T10:30:00Z',
  updatedAt: '2026-08-14T10:30:44Z',
  result: null,
  error: null,
};

const succeededJob = {
  ...runningJob,
  status: JOB_STATUS.SUCCEEDED,
  stage: JOB_STAGE.FINALIZING,
  progress: 1,
  result: {
    deck: { title: 'Road signs' },
    notes: [
      {
        clientId: 'b1a2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d',
        noteType: NOTE_TYPE.BASIC,
        fields: { front: 'What does a red triangle mean?', back: 'A warning sign' },
        sources: [{ title: 'Traffic regulations', url: 'https://example.com/signs' }],
      },
    ],
  },
};

describe('generationJobSchema', () => {
  it('parses a running job', () => {
    const parsed = generationJobSchema.parse(runningJob);

    expect(parsed.status).toBe(JOB_STATUS.RUNNING);
    expect(parsed.result).toBeNull();
  });

  it('fills array defaults on generated notes', () => {
    const parsed = generationJobSchema.parse(succeededJob);
    const note = parsed.result?.notes[0];

    expect(note?.media).toEqual([]);
    expect(note?.tags).toEqual([]);
    expect(parsed.result?.deck.tags).toEqual([]);
  });

  it('defaults a missing stage to null', () => {
    const { stage: _stage, ...withoutStage } = runningJob;

    expect(generationJobSchema.parse(withoutStage).stage).toBeNull();
  });

  it('rejects a progress value outside zero to one', () => {
    expect(() => generationJobSchema.parse({ ...runningJob, progress: 1.2 })).toThrow();
  });

  it('rejects an unknown job status', () => {
    expect(() => generationJobSchema.parse({ ...runningJob, status: 'paused' })).toThrow();
  });
});

describe('generationRequestSchema', () => {
  it('accepts a minimal request', () => {
    const parsed = generationRequestSchema.parse({
      topic: 'Irregular verbs',
      language: 'en',
      cardCount: 30,
    });

    expect(parsed.topic).toBe('Irregular verbs');
  });

  it('rejects a card count below the minimum', () => {
    expect(() =>
      generationRequestSchema.parse({ topic: 'Irregular verbs', language: 'en', cardCount: 1 }),
    ).toThrow();
  });

  it('rejects a topic shorter than three characters', () => {
    expect(() =>
      generationRequestSchema.parse({ topic: 'ab', language: 'en', cardCount: 30 }),
    ).toThrow();
  });
});

describe('problemSchema', () => {
  it('parses a rate limit problem', () => {
    const parsed = problemSchema.parse({
      type: 'https://api.example.com/problems/rate-limited',
      title: 'Rate limited',
      status: 429,
      detail: 'Too many generation jobs for this client',
      code: 'RATE_LIMITED',
      retryAfterSeconds: 60,
    });

    expect(parsed.code).toBe('RATE_LIMITED');
  });
});

describe('isTerminalJobStatus', () => {
  it('treats queued and running as non-terminal', () => {
    expect(isTerminalJobStatus(JOB_STATUS.QUEUED)).toBe(false);
    expect(isTerminalJobStatus(JOB_STATUS.RUNNING)).toBe(false);
  });

  it('treats succeeded, failed and cancelled as terminal', () => {
    expect(isTerminalJobStatus(JOB_STATUS.SUCCEEDED)).toBe(true);
    expect(isTerminalJobStatus(JOB_STATUS.FAILED)).toBe(true);
    expect(isTerminalJobStatus(JOB_STATUS.CANCELLED)).toBe(true);
  });

  it('treats an absent status as non-terminal', () => {
    expect(isTerminalJobStatus(undefined)).toBe(false);
  });
});
