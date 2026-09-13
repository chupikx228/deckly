import { safeParseTypedNote } from '@/entities/note';
import { NOTE_TYPE } from '@/shared/config';
import type { NoteRow } from '@/shared/db';

export type EditableNoteType =
  typeof NOTE_TYPE.BASIC | typeof NOTE_TYPE.BASIC_REVERSED | typeof NOTE_TYPE.CLOZE;

export const EDITABLE_NOTE_TYPES: readonly EditableNoteType[] = [
  NOTE_TYPE.BASIC,
  NOTE_TYPE.BASIC_REVERSED,
  NOTE_TYPE.CLOZE,
];

export const NOTE_TYPE_LABEL_KEY = {
  [NOTE_TYPE.BASIC]: 'noteType.basic',
  [NOTE_TYPE.BASIC_REVERSED]: 'noteType.basic_reversed',
  [NOTE_TYPE.CLOZE]: 'noteType.cloze',
} as const satisfies Record<EditableNoteType, string>;

export const NOTE_TYPE_HINT_KEY = {
  [NOTE_TYPE.BASIC]: 'note.typeHintBasic',
  [NOTE_TYPE.BASIC_REVERSED]: 'note.typeHintReversed',
  [NOTE_TYPE.CLOZE]: 'note.typeHintCloze',
} as const satisfies Record<EditableNoteType, string>;

export interface NoteFormValues {
  readonly noteType: EditableNoteType;
  readonly front: string;
  readonly back: string;
  readonly text: string;
  readonly extra: string;
}

export const EMPTY_NOTE_FORM_VALUES: NoteFormValues = {
  noteType: NOTE_TYPE.BASIC,
  front: '',
  back: '',
  text: '',
  extra: '',
};

export const toNoteFormValues = (note: NoteRow): NoteFormValues => {
  const typed = safeParseTypedNote(note.noteType, note.fields);

  if (typed === null) {
    return EMPTY_NOTE_FORM_VALUES;
  }

  if (typed.noteType === NOTE_TYPE.CLOZE) {
    return {
      ...EMPTY_NOTE_FORM_VALUES,
      noteType: NOTE_TYPE.CLOZE,
      text: typed.fields.text,
      extra: typed.fields.extra,
    };
  }

  if (typed.noteType === NOTE_TYPE.BASIC || typed.noteType === NOTE_TYPE.BASIC_REVERSED) {
    return {
      ...EMPTY_NOTE_FORM_VALUES,
      noteType: typed.noteType,
      front: typed.fields.front,
      back: typed.fields.back,
    };
  }

  return EMPTY_NOTE_FORM_VALUES;
};
