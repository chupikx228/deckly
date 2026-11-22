import type { GenerationRequest, RegenerateNoteRequest } from '@deckly/api-contract';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import { generationApi, type DocumentUpload, type UploadProgressHandler } from '@/shared/api';
import { mutationKeys, queryKeys } from '@/shared/config';
import { createId } from '@/shared/lib';

export const useCreateGeneration = () =>
  useMutation({
    mutationKey: mutationKeys.createGeneration(),
    mutationFn: (request: GenerationRequest) => generationApi.createGeneration(request, createId()),
  });

export interface UploadDocumentInput {
  readonly file: DocumentUpload;
  readonly onProgress: UploadProgressHandler;
}

export const useUploadDocument = () =>
  useMutation({
    mutationKey: mutationKeys.uploadDocument(),
    mutationFn: ({ file, onProgress }: UploadDocumentInput) =>
      generationApi.uploadDocument(file, onProgress),
  });

export const useUploadRecording = () =>
  useMutation({
    mutationKey: mutationKeys.uploadRecording(),
    mutationFn: ({ file, onProgress }: UploadDocumentInput) =>
      generationApi.uploadRecording(file, onProgress),
  });

export const useFetchArticle = () =>
  useMutation({
    mutationKey: mutationKeys.fetchArticle(),
    mutationFn: (url: string) => generationApi.fetchArticle(url),
  });

export const useFetchRecording = () =>
  useMutation({
    mutationKey: mutationKeys.fetchRecording(),
    mutationFn: (url: string) => generationApi.fetchRecording(url),
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
