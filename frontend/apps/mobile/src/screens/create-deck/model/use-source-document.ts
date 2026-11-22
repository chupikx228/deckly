import { ERROR_CODE, type Document } from '@deckly/api-contract';
import { useCallback, useMemo, useState } from 'react';

import {
  useFetchArticle,
  useFetchRecording,
  useUploadDocument,
  useUploadRecording,
} from '@/features/generate-deck';
import { ApiError } from '@/shared/api';
import { UPLOAD } from '@/shared/config';

import type { SubmitRoute } from './source-rules';

const PERCENT = 100;
const TOO_LARGE_STATUS = 413;

export type DocumentRoute = Exclude<SubmitRoute, { kind: 'topic' }>;

const tooLarge = (): ApiError =>
  new ApiError({
    type: 'about:blank',
    title: 'Document too large',
    status: TOO_LARGE_STATUS,
    code: ERROR_CODE.DOCUMENT_TOO_LARGE,
  });

export interface SourceDocument {
  readonly isUploading: boolean;
  readonly isPending: boolean;
  readonly uploadPercent: number;
  readonly error: Error | null;
  readonly resolve: (route: DocumentRoute, onReady: (document: Document) => void) => void;
  readonly reset: () => void;
}

export const useSourceDocument = (): SourceDocument => {
  const [uploadRatio, setUploadRatio] = useState(0);
  const [refusal, setRefusal] = useState<Error | null>(null);

  const uploadDocument = useUploadDocument();
  const uploadRecording = useUploadRecording();
  const fetchArticle = useFetchArticle();
  const fetchRecording = useFetchRecording();

  const isUploading = uploadDocument.isPending || uploadRecording.isPending;
  const isPending = isUploading || fetchArticle.isPending || fetchRecording.isPending;

  const resolve = useCallback(
    (route: DocumentRoute, onReady: (document: Document) => void) => {
      setRefusal(null);

      if (route.kind === 'article') {
        fetchArticle.mutate(route.url, { onSuccess: onReady });
        return;
      }

      if (route.kind === 'recordingUrl') {
        fetchRecording.mutate(route.url, { onSuccess: onReady });
        return;
      }

      const isRecording = route.kind === 'recording';
      const maxBytes = isRecording ? UPLOAD.MAX_RECORDING_BYTES : UPLOAD.MAX_BYTES;

      if (route.file.sizeBytes > maxBytes) {
        setRefusal(tooLarge());
        return;
      }

      setUploadRatio(0);

      const upload = isRecording ? uploadRecording : uploadDocument;

      upload.mutate({ file: route.file, onProgress: setUploadRatio }, { onSuccess: onReady });
    },
    [fetchArticle, fetchRecording, uploadDocument, uploadRecording],
  );

  const reset = useCallback(() => {
    setRefusal(null);
    uploadDocument.reset();
    uploadRecording.reset();
    fetchArticle.reset();
    fetchRecording.reset();
  }, [fetchArticle, fetchRecording, uploadDocument, uploadRecording]);

  return useMemo(
    () => ({
      isUploading,
      isPending,
      uploadPercent: Math.round(uploadRatio * PERCENT),
      error:
        refusal ??
        uploadDocument.error ??
        uploadRecording.error ??
        fetchArticle.error ??
        fetchRecording.error,
      resolve,
      reset,
    }),
    [
      fetchArticle.error,
      fetchRecording.error,
      isPending,
      isUploading,
      refusal,
      reset,
      resolve,
      uploadDocument.error,
      uploadRecording.error,
      uploadRatio,
    ],
  );
};
