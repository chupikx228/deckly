import {
  GENERATABLE_DOCUMENT_TYPES,
  GENERATABLE_RECORDING_TYPES,
  type DocumentContentType,
} from '@deckly/api-contract';
import { useCallback } from 'react';

import type { DocumentUpload } from '@/shared/api';

import { hasUsableSource, resolveSubmitRoute } from './source-rules';
import { useDocumentPick } from './use-document-pick';
import { useGenerationSubmit, type GenerationRequestBase } from './use-generation-submit';
import { useWizardFields, type WizardFields } from './use-wizard-fields';
import { GENERATION_SOURCE, type GenerationSource } from './wizard-options';

const toRequestBase = (fields: WizardFields): GenerationRequestBase => ({
  language: fields.contentLanguage,
  cardCount: Number(fields.cardCount),
  difficulty: fields.difficulty,
  noteTypes: [...fields.noteTypes],
  includeImages: false,
  ...(fields.instructions.trim().length > 0 ? { instructions: fields.instructions.trim() } : {}),
});

const pickableTypes = (source: GenerationSource): readonly DocumentContentType[] =>
  source === GENERATION_SOURCE.RECORDING ? GENERATABLE_RECORDING_TYPES : GENERATABLE_DOCUMENT_TYPES;

export interface GenerationForm extends WizardFields {
  readonly file: DocumentUpload | null;
  readonly uploadPercent: number;
  readonly isUploading: boolean;
  readonly isSubmitting: boolean;
  readonly canSubmit: boolean;
  readonly error: Error | null;
  readonly pickFile: () => void;
  readonly submit: (onCreated: (jobId: string) => void) => void;
}

export const useGenerationForm = (): GenerationForm => {
  const fields = useWizardFields();
  const { file, pickFile, clearFile } = useDocumentPick();
  const submission = useGenerationSubmit();

  const { setSource, source } = fields;
  const { reset } = submission;

  const changeSource = useCallback(
    (value: GenerationSource) => {
      reset();
      clearFile();
      setSource(value);
    },
    [clearFile, reset, setSource],
  );

  const choosePickedFile = useCallback(() => {
    reset();
    pickFile(pickableTypes(source));
  }, [pickFile, reset, source]);

  const canSubmit =
    hasUsableSource(fields, file) && fields.noteTypes.length > 0 && !submission.isSubmitting;

  const submit = useCallback(
    (onCreated: (jobId: string) => void) => {
      if (!canSubmit) {
        return;
      }

      const route = resolveSubmitRoute(fields, file);

      if (route === null) {
        return;
      }

      submission.submit({ base: toRequestBase(fields), route }, onCreated);
    },
    [canSubmit, fields, file, submission],
  );

  return {
    ...fields,
    file,
    uploadPercent: submission.uploadPercent,
    isUploading: submission.isUploading,
    isSubmitting: submission.isSubmitting,
    canSubmit,
    error: submission.error,
    setSource: changeSource,
    pickFile: choosePickedFile,
    submit,
  };
};
