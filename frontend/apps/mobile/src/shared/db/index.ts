export { db, DATABASE_NAME, type Database } from './client';
export { migrations } from './migrations';

export {
  decks,
  notes,
  cards,
  reviews,
  media,
  DECK_SOURCE_KIND,
  type DeckRow,
  type DeckInsert,
  type DeckSourceKind,
  type NoteRow,
  type NoteInsert,
  type CardRow,
  type CardInsert,
  type ReviewRow,
  type ReviewInsert,
  type MediaRow,
  type MediaInsert,
} from './schema';
