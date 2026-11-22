import { DIFFICULTY, LOCALE, NOTE_TYPE, type Difficulty, type Locale } from '@/shared/config';

export const GENERATION_SOURCE = {
  TOPIC: 'topic',
  DOCUMENT: 'document',
  RECORDING: 'recording',
  ARTICLE: 'article',
} as const;

export type GenerationSource = (typeof GENERATION_SOURCE)[keyof typeof GENERATION_SOURCE];

export const GENERATION_SOURCE_OPTIONS = [
  GENERATION_SOURCE.TOPIC,
  GENERATION_SOURCE.DOCUMENT,
  GENERATION_SOURCE.RECORDING,
  GENERATION_SOURCE.ARTICLE,
] as const;

export const GENERATION_SOURCE_LABEL_KEY = {
  [GENERATION_SOURCE.TOPIC]: 'wizard.sourceTopic',
  [GENERATION_SOURCE.DOCUMENT]: 'wizard.sourceDocument',
  [GENERATION_SOURCE.RECORDING]: 'wizard.sourceRecording',
  [GENERATION_SOURCE.ARTICLE]: 'wizard.sourceArticle',
} as const satisfies Record<GenerationSource, string>;

export const CARD_COUNT_OPTIONS = [10, 20, 30, 50] as const;

export type CardCountOption = (typeof CARD_COUNT_OPTIONS)[number];

export const GENERATABLE_TYPES = [
  NOTE_TYPE.BASIC,
  NOTE_TYPE.BASIC_REVERSED,
  NOTE_TYPE.CLOZE,
] as const;

export type GeneratableType = (typeof GENERATABLE_TYPES)[number];

export const NOTE_TYPE_LABEL_KEY = {
  [NOTE_TYPE.BASIC]: 'noteType.basic',
  [NOTE_TYPE.BASIC_REVERSED]: 'noteType.basic_reversed',
  [NOTE_TYPE.CLOZE]: 'noteType.cloze',
} as const satisfies Record<GeneratableType, string>;

export const DIFFICULTY_OPTIONS = [
  DIFFICULTY.BEGINNER,
  DIFFICULTY.INTERMEDIATE,
  DIFFICULTY.ADVANCED,
] as const;

export const DIFFICULTY_LABEL_KEY = {
  [DIFFICULTY.BEGINNER]: 'difficulty.beginner',
  [DIFFICULTY.INTERMEDIATE]: 'difficulty.intermediate',
  [DIFFICULTY.ADVANCED]: 'difficulty.advanced',
} as const satisfies Record<Difficulty, string>;

export const CONTENT_LANGUAGE_OPTIONS = [LOCALE.EN, LOCALE.RU] as const;

export const CONTENT_LANGUAGE_LABEL: Readonly<Record<Locale, string>> = {
  [LOCALE.EN]: 'English',
  [LOCALE.RU]: 'Русский',
};
