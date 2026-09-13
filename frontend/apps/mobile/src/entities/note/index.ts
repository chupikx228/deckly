export {
  parseTypedNote,
  safeParseTypedNote,
  NOTE_FIELD_SCHEMA,
  basicFieldsSchema,
  clozeFieldsSchema,
  type TypedNote,
  type NoteFields,
  type NoteFieldsMap,
  type ImageOcclusionRegion,
} from './model/note-fields';

export {
  deriveCardOrdinals,
  getCardFaces,
  FORWARD_ORDINAL,
  REVERSE_ORDINAL,
  type CardFaces,
} from './model/card-templates';

export {
  parseClozeDeletions,
  getClozeIndices,
  renderClozeQuestion,
  renderClozeAnswer,
  validateClozeText,
  CLOZE_ERROR,
  CLOZE_PLACEHOLDER,
  CLOZE_SYNTAX_HINT,
  CLOZE_SYNTAX_EXAMPLE,
  type ClozeDeletion,
  type ClozeError,
} from './model/cloze';

export { useNotesInDeck, useNote } from './api/use-notes';

export { NotePreview, type NotePreviewProps } from './ui/note-preview';
