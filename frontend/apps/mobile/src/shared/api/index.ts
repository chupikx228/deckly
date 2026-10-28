import { API } from '@/shared/config';

import { httpTransport } from './http-transport';
import { mockTransport } from './mock/mock-transport';
import type { GenerationTransport } from './transport';

export const generationApi: GenerationTransport = API.USE_MOCK ? mockTransport : httpTransport;

export type { GenerationTransport } from './transport';

export {
  ApiError,
  NetworkError,
  describeError,
  isApiError,
  isNetworkError,
  isRetryableError,
  toApiError,
  type ErrorDescriptor,
} from './errors';

export { getClientId } from './client-id';
export { MOCK_FAILURE_TOPIC_MARKER } from './mock/mock-transport';
