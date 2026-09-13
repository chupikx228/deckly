import type { GenerationRequest, RegenerateNoteRequest } from '@deckly/api-contract';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import { generationApi } from '@/shared/api';
import { mutationKeys, queryKeys } from '@/shared/config';
import { createId } from '@/shared/lib';

export const useCreateGeneration = () =>
  useMutation({
    mutationKey: mutationKeys.createGeneration(),
    mutationFn: (request: GenerationRequest) => generationApi.createGeneration(request, createId()),
  });

export const useCancelGeneration = (jobId: string) => {
  const queryClient = useQueryClient();

  return useMutation({
    mutationKey: mutationKeys.cancelGeneration(jobId),
    mutationFn: () => generationApi.cancelGeneration(jobId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.generation.job(jobId) });
    },
  });
};

export const useRegenerateNote = () =>
  useMutation({
    mutationFn: (request: RegenerateNoteRequest) => generationApi.regenerateNote(request),
  });
