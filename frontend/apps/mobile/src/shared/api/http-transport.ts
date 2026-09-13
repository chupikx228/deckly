import {
  generatedNoteSchema,
  generationJobCreatedSchema,
  generationJobSchema,
  healthSchema,
} from '@deckly/api-contract';
import type { z } from 'zod';

import { API } from '@/shared/config';

import { getClientId } from './client-id';
import { NetworkError, toApiError } from './errors';
import type { GenerationTransport } from './transport';

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

export const httpTransport: GenerationTransport = {
  createGeneration: (request, idempotencyKey) =>
    requestJson(
      '/generations',
      { method: 'POST', body: request, idempotencyKey },
      generationJobCreatedSchema,
    ),

  getGeneration: (jobId) =>
    requestJson(`/generations/${jobId}`, { method: 'GET' }, generationJobSchema),

  cancelGeneration: (jobId) => requestEmpty(`/generations/${jobId}/cancel`, { method: 'POST' }),

  regenerateNote: (request) =>
    requestJson('/notes/regenerate', { method: 'POST', body: request }, generatedNoteSchema),

  getHealth: () => requestJson('/health', { method: 'GET' }, healthSchema),
};
