import { DIFFICULTY, NOTE_TYPE, type Difficulty, type NoteType } from '@deckly/api-contract';
import { CARD_STATE, RATING, RATING_ORDER, type CardState, type Rating } from '@deckly/srs';

export { NOTE_TYPE, DIFFICULTY, CARD_STATE, RATING, RATING_ORDER };
export type { NoteType, Difficulty, CardState, Rating };

export const SUPPORTED_NOTE_TYPES: readonly NoteType[] = [
  NOTE_TYPE.BASIC,
  NOTE_TYPE.BASIC_REVERSED,
  NOTE_TYPE.CLOZE,
];

export const GENERATABLE_NOTE_TYPES: readonly NoteType[] = [
  NOTE_TYPE.BASIC,
  NOTE_TYPE.BASIC_REVERSED,
  NOTE_TYPE.CLOZE,
];

export const DIFFICULTY_ORDER = [
  DIFFICULTY.BEGINNER,
  DIFFICULTY.INTERMEDIATE,
  DIFFICULTY.ADVANCED,
] as const;
