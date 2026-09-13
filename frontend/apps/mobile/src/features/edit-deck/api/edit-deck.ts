import { eq } from 'drizzle-orm';

import { DECK_SOURCE_KIND, db, decks } from '@/shared/db';
import { createId } from '@/shared/lib';

export interface CreateDeckInput {
  readonly title: string;
  readonly description: string | null;
}

export const createDeck = ({ title, description }: CreateDeckInput): string => {
  const now = Date.now();
  const id = createId();

  db.insert(decks)
    .values({
      id,
      title: title.trim(),
      description: description === null ? null : description.trim(),
      sourceKind: DECK_SOURCE_KIND.MANUAL,
      isArchived: false,
      createdAt: now,
      updatedAt: now,
    })
    .run();

  return id;
};

export const renameDeck = (deckId: string, title: string): void => {
  db.update(decks)
    .set({ title: title.trim(), updatedAt: Date.now() })
    .where(eq(decks.id, deckId))
    .run();
};

export const deleteDeck = (deckId: string): void => {
  db.delete(decks).where(eq(decks.id, deckId)).run();
};
