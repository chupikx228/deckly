import type { GeneratedDeck, GeneratedNote } from '@deckly/api-contract';

import { createNewCard } from '@/entities/card';
import { deriveCardOrdinals, safeParseTypedNote } from '@/entities/note';
import { DECK_SOURCE_KIND, cards, db, decks, notes } from '@/shared/db';
import { createId } from '@/shared/lib';

export interface ImportGeneratedDeckInput {
  readonly deck: GeneratedDeck;
  readonly notes: readonly GeneratedNote[];
  readonly jobId: string;
}

export interface ImportGeneratedDeckResult {
  readonly deckId: string;
  readonly importedNotes: number;
  readonly skippedNotes: number;
}

export const importGeneratedDeck = ({
  deck,
  notes: generated,
  jobId,
}: ImportGeneratedDeckInput): ImportGeneratedDeckResult => {
  const now = Date.now();
  const deckId = createId();

  let importedNotes = 0;
  let skippedNotes = 0;

  db.transaction((tx) => {
    tx.insert(decks)
      .values({
        id: deckId,
        title: deck.title.trim(),
        description: deck.description ?? null,
        sourceKind: DECK_SOURCE_KIND.GENERATED,
        generationJobId: jobId,
        isArchived: false,
        createdAt: now,
        updatedAt: now,
      })
      .run();

    for (const item of generated) {
      const typed = safeParseTypedNote(item.noteType, item.fields);

      if (typed === null) {
        skippedNotes += 1;
        continue;
      }

      const ordinals = deriveCardOrdinals(typed);

      if (ordinals.length === 0) {
        skippedNotes += 1;
        continue;
      }

      const noteId = createId();

      tx.insert(notes)
        .values({
          id: noteId,
          deckId,
          noteType: item.noteType,
          fields: typed.fields,
          tags: item.tags,
          sources: item.sources,
          createdAt: now,
          updatedAt: now,
        })
        .run();

      tx.insert(cards)
        .values(
          ordinals.map((templateOrdinal) =>
            createNewCard({ noteId, deckId, templateOrdinal, now }),
          ),
        )
        .run();

      importedNotes += 1;
    }
  });

  return { deckId, importedNotes, skippedNotes };
};
