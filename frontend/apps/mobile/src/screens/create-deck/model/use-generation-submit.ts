import { ERROR_CODE, type GenerationRequest } from '@deckly/api-contract';
import { useCallback, useMemo, useState } from 'react';

import { useCreateGeneration, useFetchArticle, useUploadDocument } from '@/features/generate-deck';
import { ApiError, type DocumentUpload } from '@/shared/api';
import { UPLOAD } from '@/shared/config';

const PERCENT = 100;
const TOO_LARGE_STATUS = 413;

const documentTooLarge = (): ApiError =>
  new ApiError({
    type: 'about:blank',
    title: 'Document too large',
    status: TOO_LARGE_STATUS,
    code: ERROR_CODE.DOCUMENT_TOO_LARGE,
  });

export type GenerationRequestBase = Omit<GenerationRequest, 'topic' | 'documentId'>;

export interface SubmitInput {
  readonly base: GenerationRequestBase;
  readonly topic: string | null;
  readonly file: DocumentUpload | null;
  readonly articleUrl: string | null;
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
  const [uploadRatio, setUploadRatio] = useState(0);
  const [refusal, setRefusal] = useState<Error | null>(null);

  const uploadDocument = useUploadDocument();
  const fetchArticle = useFetchArticle();
  const createGeneration = useCreateGeneration();

  const isSubmitting =
    uploadDocument.isPending || fetchArticle.isPending || createGeneration.isPending;

  const submit = useCallback(
    (input: SubmitInput, onCreated: (jobId: string) => void) => {
      if (isSubmitting) {
        return;
      }

      setRefusal(null);

      const start = (request: GenerationRequest): void => {
        createGeneration.mutate(request, {
          onSuccess: (job) => {
            onCreated(job.jobId);
          },
        });
      };

      if (input.topic !== null) {
        start({ ...input.base, topic: input.topic });
        return;
      }

      if (input.articleUrl !== null) {
        fetchArticle.mutate(input.articleUrl, {
          onSuccess: (article) => {
            start({ ...input.base, documentId: article.documentId });
          },
        });

        return;
      }

      if (input.file === null) {
        return;
      }

      if (input.file.sizeBytes > UPLOAD.MAX_BYTES) {
        setRefusal(documentTooLarge());
        return;
      }

      setUploadRatio(0);

      uploadDocument.mutate(
        { file: input.file, onProgress: setUploadRatio },
        {
          onSuccess: (uploaded) => {
            start({ ...input.base, documentId: uploaded.documentId });
          },
        },
      );
    },
    [createGeneration, fetchArticle, isSubmitting, uploadDocument],
  );

  const reset = useCallback(() => {
    setRefusal(null);
    uploadDocument.reset();
    fetchArticle.reset();
    createGeneration.reset();
  }, [createGeneration, fetchArticle, uploadDocument]);

  return useMemo(
    () => ({
      isUploading: uploadDocument.isPending,
      isSubmitting,
      uploadPercent: Math.round(uploadRatio * PERCENT),
      error: refusal ?? uploadDocument.error ?? fetchArticle.error ?? createGeneration.error,
      submit,
      reset,
    }),
    [
      createGeneration.error,
      fetchArticle.error,
      isSubmitting,
      refusal,
      reset,
      submit,
      uploadDocument.error,
      uploadDocument.isPending,
      uploadRatio,
    ],
  );
};
