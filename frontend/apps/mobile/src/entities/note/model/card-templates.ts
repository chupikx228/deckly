import { NOTE_TYPE } from '@/shared/config';

import { getClozeIndices, renderClozeAnswer, renderClozeQuestion } from './cloze';
import type { TypedNote } from './note-fields';

export const FORWARD_ORDINAL = 0;
export const REVERSE_ORDINAL = 1;

export interface CardFaces {
  readonly question: string;
  readonly answer: string;
  readonly hint: string | null;
}

export const deriveCardOrdinals = (note: TypedNote): number[] => {
  switch (note.noteType) {
    case NOTE_TYPE.BASIC:
    case NOTE_TYPE.BASIC_TYPE_IN:
    case NOTE_TYPE.MULTIPLE_CHOICE:
      return [FORWARD_ORDINAL];

    case NOTE_TYPE.BASIC_REVERSED:
      return [FORWARD_ORDINAL, REVERSE_ORDINAL];

    case NOTE_TYPE.BASIC_OPTIONAL_REVERSED:
      return note.fields.addReverse.trim().length > 0
        ? [FORWARD_ORDINAL, REVERSE_ORDINAL]
        : [FORWARD_ORDINAL];

    case NOTE_TYPE.CLOZE:
      return getClozeIndices(note.fields.text);

    case NOTE_TYPE.IMAGE_OCCLUSION:
      return note.fields.regions.map((region) => region.ordinal);
  }
};

const withHint = (extra: string): string | null => (extra.trim().length > 0 ? extra : null);

export const getCardFaces = (note: TypedNote, templateOrdinal: number): CardFaces => {
  switch (note.noteType) {
    case NOTE_TYPE.BASIC:
    case NOTE_TYPE.BASIC_TYPE_IN:
    case NOTE_TYPE.BASIC_REVERSED:
    case NOTE_TYPE.BASIC_OPTIONAL_REVERSED:
      return templateOrdinal === REVERSE_ORDINAL
        ? { question: note.fields.back, answer: note.fields.front, hint: null }
        : { question: note.fields.front, answer: note.fields.back, hint: null };

    case NOTE_TYPE.CLOZE:
      return {
        question: renderClozeQuestion(note.fields.text, templateOrdinal),
        answer: renderClozeAnswer(note.fields.text, templateOrdinal),
        hint: withHint(note.fields.extra),
      };

    case NOTE_TYPE.MULTIPLE_CHOICE:
      return { question: note.fields.question, answer: note.fields.answer, hint: null };

    case NOTE_TYPE.IMAGE_OCCLUSION:
      return { question: '', answer: '', hint: withHint(note.fields.extra) };
  }
};
