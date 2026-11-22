import {
  ERROR_CODE,
  JOB_STAGE_ORDER,
  JOB_STATUS,
  type Document,
  type GeneratedNote,
  type GenerationJob,
  type GenerationRequest,
  type JobStatus,
} from '@deckly/api-contract';

import { notFound } from './mock-errors';

export const MOCK_STAGE_DURATION_MS = 1_400;
export const MOCK_FAILURE_TOPIC_MARKER = 'fail';

const TOTAL_DURATION_MS = MOCK_STAGE_DURATION_MS * JOB_STAGE_ORDER.length;

export interface MockJob {
  readonly jobId: string;
  readonly request: GenerationRequest;
  readonly createdAt: number;
  readonly notes: GeneratedNote[];
  isCancelled: boolean;
}

export const jobs = new Map<string, MockJob>();
export const documents = new Map<string, Document>();

const MOCK_DOCUMENT_SUBJECT = 'Uploaded document';

export const resolveSubject = (request: GenerationRequest): string => {
  if (request.topic !== undefined) {
    return request.topic;
  }

  const uploaded = request.documentId === undefined ? undefined : documents.get(request.documentId);

  return uploaded?.filename ?? MOCK_DOCUMENT_SUBJECT;
};
export const idempotencyKeys = new Map<string, string>();

const shouldFail = (topic: string): boolean =>
  topic.toLowerCase().includes(MOCK_FAILURE_TOPIC_MARKER);

export const resolveStatus = (job: MockJob, elapsed: number): JobStatus => {
  if (job.isCancelled) {
    return JOB_STATUS.CANCELLED;
  }

  if (elapsed < MOCK_STAGE_DURATION_MS) {
    return JOB_STATUS.QUEUED;
  }

  if (elapsed < TOTAL_DURATION_MS) {
    return JOB_STATUS.RUNNING;
  }

  return shouldFail(resolveSubject(job.request)) ? JOB_STATUS.FAILED : JOB_STATUS.SUCCEEDED;
};

export const toJob = (job: MockJob, now: number): GenerationJob => {
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
              title: resolveSubject(job.request),
              description: `Generated from "${resolveSubject(job.request)}"`,
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

export const readJob = (jobId: string): MockJob => {
  const job = jobs.get(jobId);

  if (job === undefined) {
    throw notFound();
  }

  return job;
};
