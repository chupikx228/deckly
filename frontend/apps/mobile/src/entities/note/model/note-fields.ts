import { z } from 'zod';

import { NOTE_TYPE, type NoteType } from '@/shared/config';

const nonEmpty = z.string().trim().min(1);

export const basicFieldsSchema = z.object({
  front: nonEmpty,
  back: nonEmpty,
});

export const optionalReversedFieldsSchema = z.object({
  front: nonEmpty,
  back: nonEmpty,
  addReverse: z.string().default(''),
});

export const clozeFieldsSchema = z.object({
  text: nonEmpty,
  extra: z.string().default(''),
});

export const multipleChoiceFieldsSchema = z.object({
  question: nonEmpty,
  answer: nonEmpty,
  distractors: z.array(nonEmpty).min(2).max(4),
});

export const imageOcclusionRegionSchema = z.object({
  ordinal: z.number().int().min(1),
  x: z.number().min(0).max(1),
  y: z.number().min(0).max(1),
  width: z.number().min(0).max(1),
  height: z.number().min(0).max(1),
});

export const imageOcclusionFieldsSchema = z.object({
  imageId: nonEmpty,
  regions: z.array(imageOcclusionRegionSchema).min(1),
  extra: z.string().default(''),
});

export const NOTE_FIELD_SCHEMA = {
  [NOTE_TYPE.BASIC]: basicFieldsSchema,
  [NOTE_TYPE.BASIC_REVERSED]: basicFieldsSchema,
  [NOTE_TYPE.BASIC_TYPE_IN]: basicFieldsSchema,
  [NOTE_TYPE.BASIC_OPTIONAL_REVERSED]: optionalReversedFieldsSchema,
  [NOTE_TYPE.CLOZE]: clozeFieldsSchema,
  [NOTE_TYPE.MULTIPLE_CHOICE]: multipleChoiceFieldsSchema,
  [NOTE_TYPE.IMAGE_OCCLUSION]: imageOcclusionFieldsSchema,
} as const;

export type NoteFieldsMap = {
  [Type in NoteType]: z.infer<(typeof NOTE_FIELD_SCHEMA)[Type]>;
};

export type NoteFields = NoteFieldsMap[NoteType];

export type ImageOcclusionRegion = z.infer<typeof imageOcclusionRegionSchema>;

export type TypedNote = {
  [Type in NoteType]: { readonly noteType: Type; readonly fields: NoteFieldsMap[Type] };
}[NoteType];

export const parseTypedNote = (noteType: NoteType, raw: unknown): TypedNote => {
  const fields = NOTE_FIELD_SCHEMA[noteType].parse(raw);

  return { noteType, fields } as TypedNote;
};

export const safeParseTypedNote = (noteType: NoteType, raw: unknown): TypedNote | null => {
  const result = NOTE_FIELD_SCHEMA[noteType].safeParse(raw);

  return result.success ? ({ noteType, fields: result.data } as TypedNote) : null;
};
