import {
  DOCUMENT_CONTENT_TYPE,
  GENERATABLE_DOCUMENT_TYPES,
  GENERATABLE_RECORDING_TYPES,
  JOB_STATUS,
  NOTE_TYPE,
  type Document,
} from '@deckly/api-contract';

import { UPLOAD } from '@/shared/config';
import { createId } from '@/shared/lib';

import type { DocumentUpload, GenerationTransport, UploadProgressHandler } from '../transport';
import {
  documentRegenerationUnsupported,
  documentTooLarge,
  recordingTooLong,
  recordingUnavailable,
  unsupportedArticleUrl,
  unsupportedDocumentType,
  unsupportedRecordingUrl,
  validationFailed,
} from './mock-errors';
import {
  documents,
  idempotencyKeys,
  jobs,
  readJob,
  resolveStatus,
  resolveSubject,
  toJob,
} from './mock-jobs';
import { createMockNotes } from './mock-notes';
import {
  MOCK_ARTICLE_MS,
  MOCK_ARTICLE_SIZE,
  MOCK_LONG_RECORDING_MARKER,
  MOCK_RECORDING_MS,
  MOCK_RECORDING_SECONDS,
  MOCK_RECORDING_SIZE,
  MOCK_UNAVAILABLE_VIDEO_MARKER,
  isArticleUrl,
  isYouTubeVideoUrl,
  simulateUpload,
  toContentType,
  toSourceFilename,
  wait,
} from './mock-sources';

export { MOCK_FAILURE_TOPIC_MARKER, MOCK_STAGE_DURATION_MS } from './mock-jobs';

export const mockTransport: GenerationTransport = {
  uploadDocument: async (file: DocumentUpload, onProgress: UploadProgressHandler) => {
    if (file.sizeBytes > UPLOAD.MAX_BYTES) {
      throw documentTooLarge();
    }

    const contentType = toContentType(file, GENERATABLE_DOCUMENT_TYPES);

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

  uploadRecording: async (file: DocumentUpload, onProgress: UploadProgressHandler) => {
    if (file.sizeBytes > UPLOAD.MAX_RECORDING_BYTES) {
      throw documentTooLarge();
    }

    const contentType = toContentType(file, GENERATABLE_RECORDING_TYPES);

    if (contentType === undefined) {
      throw unsupportedDocumentType();
    }

    if (file.name.toLowerCase().includes(MOCK_LONG_RECORDING_MARKER)) {
      throw recordingTooLong();
    }

    await simulateUpload(onProgress);

    const uploaded: Document = {
      documentId: createId(),
      filename: file.name,
      sizeBytes: file.sizeBytes,
      contentType,
      uploadedAt: new Date().toISOString(),
      durationSeconds: MOCK_RECORDING_SECONDS,
    };

    documents.set(uploaded.documentId, uploaded);

    return uploaded;
  },

  fetchRecording: async (url: string) => {
    const trimmed = url.trim();

    if (!isYouTubeVideoUrl(trimmed)) {
      throw unsupportedRecordingUrl();
    }

    if (trimmed.toLowerCase().includes(MOCK_UNAVAILABLE_VIDEO_MARKER)) {
      throw recordingUnavailable();
    }

    if (trimmed.toLowerCase().includes(MOCK_LONG_RECORDING_MARKER)) {
      throw recordingTooLong();
    }

    await wait(MOCK_RECORDING_MS);

    const recording: Document = {
      documentId: createId(),
      filename: toSourceFilename(trimmed),
      sizeBytes: MOCK_RECORDING_SIZE,
      contentType: DOCUMENT_CONTENT_TYPE.M4A,
      uploadedAt: new Date().toISOString(),
      durationSeconds: MOCK_RECORDING_SECONDS,
      sourceUrl: trimmed,
    };

    documents.set(recording.documentId, recording);

    return recording;
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

    if (!isArticleUrl(trimmed)) {
      throw unsupportedArticleUrl();
    }

    if (/\.pdf(?:[?#]|$)/i.test(trimmed)) {
      throw unsupportedDocumentType();
    }

    await wait(MOCK_ARTICLE_MS);

    const article: Document = {
      documentId: createId(),
      filename: toSourceFilename(trimmed),
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
