import { useCallback, useState } from 'react';

import type { DocumentUpload } from '@/shared/api';
import { DIFFICULTY, GENERATION, NOTE_TYPE, type Difficulty, type Locale } from '@/shared/config';
import { useLocale } from '@/shared/lib';

import { useDocumentPick } from './use-document-pick';
import { useGenerationSubmit, type GenerationRequestBase } from './use-generation-submit';
import { GENERATION_SOURCE, type GeneratableType, type GenerationSource } from './wizard-options';

const MIN_TOPIC_LENGTH = 3;

interface RequestValues {
  readonly contentLanguage: Locale;
  readonly cardCount: string;
  readonly difficulty: Difficulty;
  readonly noteTypes: readonly GeneratableType[];
  readonly instructions: string;
}

const toRequestBase = (values: RequestValues): GenerationRequestBase => ({
  language: values.contentLanguage,
  cardCount: Number(values.cardCount),
  difficulty: values.difficulty,
  noteTypes: [...values.noteTypes],
  includeImages: false,
  ...(values.instructions.trim().length > 0 ? { instructions: values.instructions.trim() } : {}),
});

export interface GenerationForm {
  readonly source: GenerationSource;
  readonly topic: string;
  readonly cardCount: string;
  readonly difficulty: Difficulty;
  readonly noteTypes: readonly GeneratableType[];
  readonly contentLanguage: Locale;
  readonly instructions: string;
  readonly file: DocumentUpload | null;
  readonly uploadPercent: number;
  readonly isUploading: boolean;
  readonly isSubmitting: boolean;
  readonly canSubmit: boolean;
  readonly error: Error | null;
  readonly setSource: (value: GenerationSource) => void;
  readonly setTopic: (value: string) => void;
  readonly setCardCount: (value: string) => void;
  readonly setDifficulty: (value: Difficulty) => void;
  readonly setContentLanguage: (value: Locale) => void;
  readonly setInstructions: (value: string) => void;
  readonly toggleNoteType: (type: GeneratableType) => void;
  readonly pickFile: () => void;
  readonly submit: (onCreated: (jobId: string) => void) => void;
}

export const useGenerationForm = (): GenerationForm => {
  const locale = useLocale();

  const [source, setSource] = useState<GenerationSource>(GENERATION_SOURCE.TOPIC);
  const [topic, setTopic] = useState('');
  const [cardCount, setCardCount] = useState<string>(String(GENERATION.DEFAULT_CARD_COUNT));
  const [difficulty, setDifficulty] = useState<Difficulty>(DIFFICULTY.INTERMEDIATE);
  const [noteTypes, setNoteTypes] = useState<readonly GeneratableType[]>([NOTE_TYPE.BASIC]);
  const [contentLanguage, setContentLanguage] = useState<Locale>(locale);
  const [instructions, setInstructions] = useState('');

  const { file, pickFile } = useDocumentPick();
  const submission = useGenerationSubmit();

  const isTopicSource = source === GENERATION_SOURCE.TOPIC;
  const hasSource = isTopicSource ? topic.trim().length >= MIN_TOPIC_LENGTH : file !== null;

  const changeSource = useCallback(
    (value: GenerationSource) => {
      submission.reset();
      setSource(value);
    },
    [submission],
  );

  const choosePickedFile = useCallback(() => {
    submission.reset();
    pickFile();
  }, [pickFile, submission]);

  const toggleNoteType = useCallback((type: GeneratableType) => {
    setNoteTypes((current) =>
      current.includes(type) ? current.filter((item) => item !== type) : [...current, type],
    );
  }, []);

  const submit = useCallback(
    (onCreated: (jobId: string) => void) => {
      submission.submit(
        {
          base: toRequestBase({ contentLanguage, cardCount, difficulty, noteTypes, instructions }),
          topic: isTopicSource ? topic.trim() : null,
          file,
        },
        onCreated,
      );
    },
    [
      cardCount,
      contentLanguage,
      difficulty,
      file,
      instructions,
      isTopicSource,
      noteTypes,
      submission,
      topic,
    ],
  );

  return {
    source,
    topic,
    cardCount,
    difficulty,
    noteTypes,
    contentLanguage,
    instructions,
    file,
    uploadPercent: submission.uploadPercent,
    isUploading: submission.isUploading,
    isSubmitting: submission.isSubmitting,
    canSubmit: hasSource && noteTypes.length > 0 && !submission.isSubmitting,
    error: submission.error,
    setSource: changeSource,
    setTopic,
    setCardCount,
    setDifficulty,
    setContentLanguage,
    setInstructions,
    toggleNoteType,
    pickFile: choosePickedFile,
    submit,
  };
};
