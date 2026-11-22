import { useCallback } from 'react';

import type { DocumentUpload } from '@/shared/api';

import { hasUsableSource } from './source-rules';
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
  const { file, pickFile } = useDocumentPick();
  const submission = useGenerationSubmit();

  const { setSource } = fields;
  const { reset } = submission;

  const changeSource = useCallback(
    (value: GenerationSource) => {
      reset();
      setSource(value);
    },
    [reset, setSource],
  );

  const choosePickedFile = useCallback(() => {
    reset();
    pickFile();
  }, [pickFile, reset]);

  const canSubmit =
    hasUsableSource(fields, file) && fields.noteTypes.length > 0 && !submission.isSubmitting;

  const submit = useCallback(
    (onCreated: (jobId: string) => void) => {
      if (!canSubmit) {
        return;
      }

      submission.submit(
        {
          base: toRequestBase(fields),
          topic: fields.source === GENERATION_SOURCE.TOPIC ? fields.topic.trim() : null,
          articleUrl: fields.source === GENERATION_SOURCE.ARTICLE ? fields.articleUrl.trim() : null,
          file,
        },
        onCreated,
      );
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
