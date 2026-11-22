import type { GenerationRequest } from '@deckly/api-contract';
import { useCallback, useMemo } from 'react';

import { useCreateGeneration } from '@/features/generate-deck';

import type { SubmitRoute } from './source-rules';
import { useSourceDocument } from './use-source-document';

export type GenerationRequestBase = Omit<GenerationRequest, 'topic' | 'documentId'>;

export interface SubmitInput {
  readonly base: GenerationRequestBase;
  readonly route: SubmitRoute;
}

export interface GenerationSubmit {
  readonly isUploading: boolean;
  readonly isSubmitting: boolean;
  readonly uploadPercent: number;
  readonly error: Error | null;
  readonly submit: (input: SubmitInput, onCreated: (jobId: string) => void) => void;
  readonly reset: () => void;
}

export const useGenerationSubmit = (): GenerationSubmit => {
  const source = useSourceDocument();
  const createGeneration = useCreateGeneration();

  const isSubmitting = source.isPending || createGeneration.isPending;

  const submit = useCallback(
    ({ base, route }: SubmitInput, onCreated: (jobId: string) => void) => {
      if (isSubmitting) {
        return;
      }

      const start = (request: GenerationRequest): void => {
        createGeneration.mutate(request, {
          onSuccess: (job) => {
            onCreated(job.jobId);
          },
        });
      };

      if (route.kind === 'topic') {
        start({ ...base, topic: route.topic });
        return;
      }

      source.resolve(route, (document) => {
        start({ ...base, documentId: document.documentId });
      });
    },
    [createGeneration, isSubmitting, source],
  );

  const reset = useCallback(() => {
    source.reset();
    createGeneration.reset();
  }, [createGeneration, source]);

  return useMemo(
    () => ({
      isUploading: source.isUploading,
      isSubmitting,
      uploadPercent: source.uploadPercent,
      error: source.error ?? createGeneration.error,
      submit,
      reset,
    }),
    [
      createGeneration.error,
      isSubmitting,
      reset,
      source.error,
      source.isUploading,
      source.uploadPercent,
      submit,
    ],
  );
};
