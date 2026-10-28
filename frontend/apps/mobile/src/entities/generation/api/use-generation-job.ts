import { isTerminalJobStatus } from '@deckly/api-contract';
import { useQuery } from '@tanstack/react-query';

import { generationApi, isRetryableError } from '@/shared/api';
import { GENERATION, queryKeys } from '@/shared/config';

export const useGenerationJob = (jobId: string) =>
  useQuery({
    queryKey: queryKeys.generation.job(jobId),
    queryFn: () => generationApi.getGeneration(jobId),
    refetchInterval: (query) => {
      if (isTerminalJobStatus(query.state.data?.status)) {
        return false;
      }

      if (query.state.error !== null && !isRetryableError(query.state.error)) {
        return false;
      }

      return GENERATION.POLL_INTERVAL_MS;
    },
    staleTime: 0,
    retry: (failureCount, error) =>
      isRetryableError(error) && failureCount < GENERATION.MAX_POLL_RETRIES,
  });
