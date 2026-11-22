import {
  DOCUMENT_CONTENT_TYPE,
  ERROR_CODE,
  GENERATABLE_DOCUMENT_TYPES,
  JOB_STAGE_ORDER,
  JOB_STATUS,
  NOTE_TYPE,
  type GeneratedNote,
  type GenerationJob,
  type Document,
  type DocumentContentType,
  type GenerationRequest,
  type JobStatus,
} from '@deckly/api-contract';

import { UPLOAD } from '@/shared/config';
import { createId } from '@/shared/lib';

import { ApiError } from '../errors';
import type { DocumentUpload, GenerationTransport, UploadProgressHandler } from '../transport';
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
const documents = new Map<string, Document>();

const MOCK_DOCUMENT_SUBJECT = 'Uploaded document';
const MOCK_UPLOAD_STEPS = 5;
const MOCK_UPLOAD_STEP_MS = 160;

const resolveSubject = (request: GenerationRequest): string => {
  if (request.topic !== undefined) {
    return request.topic;
  }

  const uploaded = request.documentId === undefined ? undefined : documents.get(request.documentId);

  return uploaded?.filename ?? MOCK_DOCUMENT_SUBJECT;
};
const idempotencyKeys = new Map<string, string>();

const notFound = (): ApiError =>
  new ApiError({
    type: 'about:blank',
    title: 'Job not found',
    status: 404,
    code: ERROR_CODE.JOB_NOT_FOUND,
  });

const documentTooLarge = (): ApiError =>
  new ApiError({
    type: 'about:blank',
    title: 'Document too large',
    status: 413,
    code: ERROR_CODE.DOCUMENT_TOO_LARGE,
  });

const unsupportedArticleUrl = (): ApiError =>
  new ApiError({
    type: 'about:blank',
    title: 'Unsupported article URL',
    status: 422,
    code: ERROR_CODE.UNSUPPORTED_ARTICLE_URL,
  });

const unsupportedDocumentType = (): ApiError =>
  new ApiError({
    type: 'about:blank',
    title: 'Unsupported document type',
    status: 415,
    code: ERROR_CODE.UNSUPPORTED_DOCUMENT_TYPE,
  });

const EXTENSION_CONTENT_TYPE: Readonly<Record<string, DocumentContentType>> = {
  pdf: DOCUMENT_CONTENT_TYPE.PDF,
  pptx: DOCUMENT_CONTENT_TYPE.PPTX,
};

const toContentType = (file: DocumentUpload): DocumentContentType | undefined => {
  const declared = GENERATABLE_DOCUMENT_TYPES.find((type) => type === file.mimeType);

  if (declared !== undefined) {
    return declared;
  }

  return EXTENSION_CONTENT_TYPE[file.name.split('.').pop()?.toLowerCase() ?? ''];
};

const MOCK_ARTICLE_MS = 900;
const MOCK_ARTICLE_SIZE = 48_000;

const ARTICLE_URL = /^https?:\/\/[^\s/?#@]+\.[^\s/?#@]+(?:[/?#]\S*)?$/i;

const wait = (ms: number): Promise<void> =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

const toArticleFilename = (url: string): string => {
  const withoutScheme = url.replace(/^https?:\/\//i, '');

  return withoutScheme.split('/')[0] ?? withoutScheme;
};

const simulateUpload = (onProgress: UploadProgressHandler): Promise<void> =>
  new Promise((resolve) => {
    let step = 0;

    const tick = (): void => {
      step += 1;
      onProgress(step / MOCK_UPLOAD_STEPS);

      if (step >= MOCK_UPLOAD_STEPS) {
        resolve();
        return;
      }

      setTimeout(tick, MOCK_UPLOAD_STEP_MS);
    };

    setTimeout(tick, MOCK_UPLOAD_STEP_MS);
  });

const validationFailed = (title: string): ApiError =>
  new ApiError({
    type: 'about:blank',
    title,
    status: 400,
    code: ERROR_CODE.VALIDATION_FAILED,
  });

const documentRegenerationUnsupported = (): ApiError =>
  new ApiError({
    type: 'about:blank',
    title: 'Document regeneration unsupported',
    status: 422,
    code: ERROR_CODE.DOCUMENT_REGENERATION_UNSUPPORTED,
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

  return shouldFail(resolveSubject(job.request)) ? JOB_STATUS.FAILED : JOB_STATUS.SUCCEEDED;
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

const readJob = (jobId: string): MockJob => {
  const job = jobs.get(jobId);

  if (job === undefined) {
    throw notFound();
  }

  return job;
};

export const mockTransport: GenerationTransport = {
  uploadDocument: async (file: DocumentUpload, onProgress: UploadProgressHandler) => {
    if (file.sizeBytes > UPLOAD.MAX_BYTES) {
      throw documentTooLarge();
    }

    const contentType = toContentType(file);

    if (contentType === undefined) {
      throw unsupportedDocumentType();
    }

    await simulateUpload(onProgress);

    const uploaded: Document = {
      documentId: createId(),
      filename: file.name,
      sizeBytes: file.sizeBytes,
      contentType,
      uploadedAt: new Date().toISOString(),
    };

    documents.set(uploaded.documentId, uploaded);

    return uploaded;
  },

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
      notes: createMockNotes(request, resolveSubject(request)),
      isCancelled: false,
    });
    idempotencyKeys.set(idempotencyKey, jobId);

    return Promise.resolve({
      jobId,
      status: JOB_STATUS.QUEUED,
      createdAt: new Date(createdAt).toISOString(),
    });
  },

  fetchArticle: async (url: string) => {
    const trimmed = url.trim();

    if (!ARTICLE_URL.test(trimmed)) {
      throw unsupportedArticleUrl();
    }

    if (/\.pdf(?:[?#]|$)/i.test(trimmed)) {
      throw unsupportedDocumentType();
    }

    await wait(MOCK_ARTICLE_MS);

    const article: Document = {
      documentId: createId(),
      filename: toArticleFilename(trimmed),
      sizeBytes: MOCK_ARTICLE_SIZE,
      contentType: DOCUMENT_CONTENT_TYPE.TXT,
      uploadedAt: new Date().toISOString(),
      sourceUrl: trimmed,
    };

    documents.set(article.documentId, article);

    return article;
  },

  getGeneration: (jobId) => Promise.resolve(toJob(readJob(jobId), Date.now())),

  cancelGeneration: (jobId) => {
    readJob(jobId).isCancelled = true;

    return Promise.resolve();
  },

  regenerateNote: (request) => {
    if ((request.topic === undefined) === (request.documentId === undefined)) {
      return Promise.reject(validationFailed('Exactly one of topic and documentId is required'));
    }

    if (request.topic === undefined) {
      return Promise.reject(documentRegenerationUnsupported());
    }

    return Promise.resolve(
      createMockNotes(
        {
          topic: request.topic,
          language: request.language,
          cardCount: 5,
          noteTypes: [request.noteType],
        },
        request.topic,
      )[0] ?? {
        clientId: createId(),
        noteType: NOTE_TYPE.BASIC,
        fields: { front: request.topic, back: 'Regenerated answer' },
        media: [],
        sources: [],
        tags: [],
      },
    );
  },

  getHealth: () => Promise.resolve({ status: 'ok', version: 'mock' }),
};
