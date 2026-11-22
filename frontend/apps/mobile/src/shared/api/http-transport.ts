import {
  articleUrlRequestSchema,
  documentSchema,
  generatedNoteSchema,
  generationJobCreatedSchema,
  generationJobSchema,
  generationRequestSchema,
  healthSchema,
  regenerateNoteRequestSchema,
  type Document,
} from '@deckly/api-contract';
import type { z } from 'zod';

import { API, UPLOAD } from '@/shared/config';

import { getClientId } from './client-id';
import { NetworkError, toApiError } from './errors';
import type { DocumentUpload, GenerationTransport, UploadProgressHandler } from './transport';

interface RequestOptions {
  readonly method: 'GET' | 'POST';
  readonly body?: unknown;
  readonly idempotencyKey?: string;
}

const buildHeaders = (options: RequestOptions): Record<string, string> => {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'X-Client-Id': getClientId(),
  };

  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }

  if (options.idempotencyKey !== undefined) {
    headers['Idempotency-Key'] = options.idempotencyKey;
  }

  return headers;
};

const send = async (path: string, options: RequestOptions): Promise<Response> => {
  const controller = new AbortController();
  const timer = setTimeout(() => {
    controller.abort();
  }, API.TIMEOUT_MS);

  try {
    return await fetch(`${API.BASE_URL}${path}`, {
      method: options.method,
      headers: buildHeaders(options),
      signal: controller.signal,
      ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
    });
  } catch (cause) {
    throw new NetworkError(cause);
  } finally {
    clearTimeout(timer);
  }
};

const parseJson = async (response: Response): Promise<unknown> => {
  try {
    return await response.json();
  } catch {
    return null;
  }
};

const requestJson = async <Schema extends z.ZodType>(
  path: string,
  options: RequestOptions,
  schema: Schema,
): Promise<z.infer<Schema>> => {
  const response = await send(path, options);
  const body = await parseJson(response);

  if (!response.ok) {
    throw toApiError(response.status, body);
  }

  return schema.parse(body);
};

const requestEmpty = async (path: string, options: RequestOptions): Promise<void> => {
  const response = await send(path, options);

  if (!response.ok) {
    throw toApiError(response.status, await parseJson(response));
  }
};

const parseText = (text: string): unknown => {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
};

const postDocument = (file: DocumentUpload, onProgress: UploadProgressHandler): Promise<Document> =>
  new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const form = new FormData();

    form.append('file', { uri: file.uri, name: file.name, type: file.mimeType });

    request.open('POST', `${API.BASE_URL}/documents`);
    request.setRequestHeader('Accept', 'application/json');
    request.setRequestHeader('X-Client-Id', getClientId());
    request.timeout = UPLOAD.TIMEOUT_MS;

    request.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(event.loaded / event.total);
      }
    };

    request.onload = () => {
      const body = parseText(request.responseText);

      if (request.status < 200 || request.status >= 300) {
        reject(toApiError(request.status, body));
        return;
      }

      const parsed = documentSchema.safeParse(body);

      if (!parsed.success) {
        reject(parsed.error);
        return;
      }

      resolve(parsed.data);
    };

    request.onerror = () => {
      reject(new NetworkError(new Error('Upload failed')));
    };

    request.ontimeout = () => {
      reject(new NetworkError(new Error('Upload timed out')));
    };

    request.onabort = () => {
      reject(new NetworkError(new Error('Upload aborted')));
    };

    request.send(form);
  });

export const httpTransport: GenerationTransport = {
  uploadDocument: postDocument,

  fetchArticle: (url) =>
    requestJson(
      '/documents/from-url',
      { method: 'POST', body: articleUrlRequestSchema.parse({ url }) },
      documentSchema,
    ),

  createGeneration: (request, idempotencyKey) =>
    requestJson(
      '/generations',
      { method: 'POST', body: generationRequestSchema.parse(request), idempotencyKey },
      generationJobCreatedSchema,
    ),

  getGeneration: (jobId) =>
    requestJson(`/generations/${jobId}`, { method: 'GET' }, generationJobSchema),

  cancelGeneration: (jobId) => requestEmpty(`/generations/${jobId}/cancel`, { method: 'POST' }),

  regenerateNote: (request) =>
    requestJson(
      '/notes/regenerate',
      { method: 'POST', body: regenerateNoteRequestSchema.parse(request) },
      generatedNoteSchema,
    ),

  getHealth: () => requestJson('/health', { method: 'GET' }, healthSchema),
};
