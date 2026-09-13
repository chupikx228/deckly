import { getCardFaces, safeParseTypedNote, type CardFaces } from '@/entities/note';
import type { StudyCard } from '@/features/study-session';

export const EMPTY_FACES: CardFaces = { question: '', answer: '', hint: null };

export const getStudyFaces = (item: StudyCard): CardFaces => {
  const typed = safeParseTypedNote(item.note.noteType, item.note.fields);

  return typed === null ? EMPTY_FACES : getCardFaces(typed, item.card.templateOrdinal);
};
