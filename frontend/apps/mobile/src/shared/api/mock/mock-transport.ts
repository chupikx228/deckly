import {
  ERROR_CODE,
  JOB_STAGE_ORDER,
  JOB_STATUS,
  NOTE_TYPE,
  type GeneratedNote,
  type GenerationJob,
  type GenerationRequest,
  type JobStatus,
} from '@deckly/api-contract';

import { createId } from '@/shared/lib';

import { ApiError } from '../errors';
import type { GenerationTransport } from '../transport';
import { createMockNotes } from './mock-notes';

export const MOCK_STAGE_DURATION_MS = 1_400;
export const MOCK_FAILURE_TOPIC_MARKER = 'fail';

const TOTAL_DURATION_MS = MOCK_STAGE_DURATION_MS * JOB_STAGE_ORDER.length;

interface MockJob {
  readonly jobId: string;
  readonly request: GenerationRequest;
  readonly createdAt: number;
  readonly notes: GeneratedNote[];
  isCancelled: boolean;
}

const jobs = new Map<string, MockJob>();
const idempotencyKeys = new Map<string, string>();

const notFound = (): ApiError =>
  new ApiError({
    type: 'about:blank',
    title: 'Job not found',
    status: 404,
    code: ERROR_CODE.JOB_NOT_FOUND,
  });

const shouldFail = (topic: string): boolean =>
  topic.toLowerCase().includes(MOCK_FAILURE_TOPIC_MARKER);

const resolveStatus = (job: MockJob, elapsed: number): JobStatus => {
  if (job.isCancelled) {
    return JOB_STATUS.CANCELLED;
  }

  if (elapsed < MOCK_STAGE_DURATION_MS) {
    return JOB_STATUS.QUEUED;
  }

  if (elapsed < TOTAL_DURATION_MS) {
    return JOB_STATUS.RUNNING;
  }

  return shouldFail(job.request.topic) ? JOB_STATUS.FAILED : JOB_STATUS.SUCCEEDED;
};

const toJob = (job: MockJob, now: number): GenerationJob => {
  const elapsed = now - job.createdAt;
  const status = resolveStatus(job, elapsed);
  const stageIndex = Math.min(
    Math.floor(elapsed / MOCK_STAGE_DURATION_MS),
    JOB_STAGE_ORDER.length - 1,
  );

  return {
    jobId: job.jobId,
    status,
    stage: status === JOB_STATUS.SUCCEEDED ? null : (JOB_STAGE_ORDER[stageIndex] ?? null),
    progress: Math.min(elapsed / TOTAL_DURATION_MS, 1),
    createdAt: new Date(job.createdAt).toISOString(),
    updatedAt: new Date(now).toISOString(),
    result:
      status === JOB_STATUS.SUCCEEDED
        ? {
            deck: {
              title: job.request.topic,
              description: `Generated from "${job.request.topic}"`,
              tags: [],
            },
            notes: job.notes,
          }
        : null,
    error:
      status === JOB_STATUS.FAILED
        ? {
            type: 'about:blank',
            title: 'Generation failed',
            status: 200,
            code: ERROR_CODE.GENERATION_FAILED,
          }
        : null,
  };
};

const readJob = (jobId: string): MockJob => {
  const job = jobs.get(jobId);

  if (job === undefined) {
    throw notFound();
  }

  return job;
};

export const mockTransport: GenerationTransport = {
  createGeneration: (request, idempotencyKey) => {
    const existing = idempotencyKeys.get(idempotencyKey);

    if (existing !== undefined) {
      const job = readJob(existing);

      return Promise.resolve({
        jobId: job.jobId,
        status: resolveStatus(job, Date.now() - job.createdAt),
        createdAt: new Date(job.createdAt).toISOString(),
      });
    }

    const jobId = createId();
    const createdAt = Date.now();

    jobs.set(jobId, {
      jobId,
      request,
      createdAt,
      notes: createMockNotes(request),
      isCancelled: false,
    });
    idempotencyKeys.set(idempotencyKey, jobId);

    return Promise.resolve({
      jobId,
      status: JOB_STATUS.QUEUED,
      createdAt: new Date(createdAt).toISOString(),
    });
  },

  getGeneration: (jobId) => Promise.resolve(toJob(readJob(jobId), Date.now())),

  cancelGeneration: (jobId) => {
    readJob(jobId).isCancelled = true;

    return Promise.resolve();
  },

  regenerateNote: (request) =>
    Promise.resolve(
      createMockNotes({
        topic: request.topic,
        language: request.language,
        cardCount: 5,
        noteTypes: [request.noteType],
      })[0] ?? {
        clientId: createId(),
        noteType: NOTE_TYPE.BASIC,
        fields: { front: request.topic, back: 'Regenerated answer' },
        media: [],
        sources: [],
        tags: [],
      },
    ),

  getHealth: () => Promise.resolve({ status: 'ok', version: 'mock' }),
};
