import { isTerminalJobStatus } from '@deckly/api-contract';
import { useQuery } from '@tanstack/react-query';

import { generationApi } from '@/shared/api';
import { GENERATION, queryKeys } from '@/shared/config';

export const useGenerationJob = (jobId: string) =>
  useQuery({
    queryKey: queryKeys.generation.job(jobId),
    queryFn: () => generationApi.getGeneration(jobId),
    refetchInterval: (query) =>
      isTerminalJobStatus(query.state.data?.status) ? false : GENERATION.POLL_INTERVAL_MS,
    staleTime: 0,
    retry: 1,
  });
