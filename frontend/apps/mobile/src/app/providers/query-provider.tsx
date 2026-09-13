import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useState } from 'react';

import { QUERY_DEFAULTS } from '@/shared/config';

const createQueryClient = (): QueryClient =>
  new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: QUERY_DEFAULTS.STALE_TIME_MS,
        retry: QUERY_DEFAULTS.RETRY_COUNT,
        retryDelay: (attempt) => Math.min(1_000 * 2 ** attempt, QUERY_DEFAULTS.MAX_RETRY_DELAY_MS),
        refetchOnWindowFocus: false,
      },
      mutations: {
        retry: 0,
      },
    },
  });

export interface QueryProviderProps {
  children: ReactNode;
}

export const QueryProvider = ({ children }: QueryProviderProps) => {
  const [client] = useState(createQueryClient);

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
};
