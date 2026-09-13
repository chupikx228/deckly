import { and, eq, inArray } from 'drizzle-orm';

import { createNewCard, diffCardOrdinals } from '@/entities/card';
import { deriveCardOrdinals, parseTypedNote } from '@/entities/note';
import type { NoteType } from '@/shared/config';
import { cards, db, decks, notes } from '@/shared/db';
import { createId } from '@/shared/lib';

export interface SaveNoteInput {
  readonly noteId: string | null;
  readonly deckId: string;
  readonly noteType: NoteType;
  readonly fields: Record<string, unknown>;
  readonly tags?: string[];
}

export const saveNote = ({ noteId, deckId, noteType, fields, tags }: SaveNoteInput): string => {
  const typed = parseTypedNote(noteType, fields);
  const nextOrdinals = deriveCardOrdinals(typed);
  const now = Date.now();
  const id = noteId ?? createId();

  db.transaction((tx) => {
    if (noteId === null) {
      tx.insert(notes)
        .values({
          id,
          deckId,
          noteType,
          fields: typed.fields,
          tags: tags ?? [],
          sources: [],
          createdAt: now,
          updatedAt: now,
        })
        .run();
    } else {
      tx.update(notes)
        .set({ noteType, fields: typed.fields, tags: tags ?? [], updatedAt: now })
        .where(eq(notes.id, id))
        .run();
    }

    const existing = tx
      .select({ templateOrdinal: cards.templateOrdinal })
      .from(cards)
      .where(eq(cards.noteId, id))
      .all();

    const { toCreate, toDelete } = diffCardOrdinals(
      existing.map((row) => row.templateOrdinal),
      nextOrdinals,
    );

    if (toDelete.length > 0) {
      tx.delete(cards)
        .where(and(eq(cards.noteId, id), inArray(cards.templateOrdinal, toDelete)))
        .run();
    }

    if (toCreate.length > 0) {
      tx.insert(cards)
        .values(
          toCreate.map((templateOrdinal) =>
            createNewCard({ noteId: id, deckId, templateOrdinal, now }),
          ),
        )
        .run();
    }

    tx.update(decks).set({ updatedAt: now }).where(eq(decks.id, deckId)).run();
  });

  return id;
};

export const deleteNote = (noteId: string): void => {
  db.delete(notes).where(eq(notes.id, noteId)).run();
};
