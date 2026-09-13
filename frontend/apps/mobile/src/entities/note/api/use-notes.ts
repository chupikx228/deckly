import { asc, eq } from 'drizzle-orm';
import { useLiveQuery } from 'drizzle-orm/expo-sqlite';

import { db, notes } from '@/shared/db';

export const useNotesInDeck = (deckId: string) =>
  useLiveQuery(
    db.select().from(notes).where(eq(notes.deckId, deckId)).orderBy(asc(notes.createdAt)),
    [deckId],
  );

export const useNote = (noteId: string) =>
  useLiveQuery(db.select().from(notes).where(eq(notes.id, noteId)).limit(1), [noteId]);
