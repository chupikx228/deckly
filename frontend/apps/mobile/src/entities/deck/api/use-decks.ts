import { desc, eq, sql } from 'drizzle-orm';
import { useLiveQuery } from 'drizzle-orm/expo-sqlite';

import { cards, db, decks } from '@/shared/db';

const summaryColumns = (cutoff: number) => ({
  id: decks.id,
  title: decks.title,
  description: decks.description,
  updatedAt: decks.updatedAt,
  totalCards: sql<number>`count(${cards.id})`,
  dueCards: sql<number>`coalesce(sum(case when ${cards.id} is not null and ${cards.isSuspended} = 0 and ${cards.due} <= ${cutoff} then 1 else 0 end), 0)`,
});

export const useDeckSummaries = (cutoff: number) =>
  useLiveQuery(
    db
      .select(summaryColumns(cutoff))
      .from(decks)
      .leftJoin(cards, eq(cards.deckId, decks.id))
      .where(eq(decks.isArchived, false))
      .groupBy(decks.id)
      .orderBy(desc(decks.updatedAt)),
    [cutoff],
  );

export const useRecentDeckSummaries = (cutoff: number, limit: number) =>
  useLiveQuery(
    db
      .select(summaryColumns(cutoff))
      .from(decks)
      .leftJoin(cards, eq(cards.deckId, decks.id))
      .where(eq(decks.isArchived, false))
      .groupBy(decks.id)
      .orderBy(desc(decks.updatedAt))
      .limit(limit),
    [cutoff, limit],
  );

export const useDeck = (deckId: string) =>
  useLiveQuery(db.select().from(decks).where(eq(decks.id, deckId)).limit(1), [deckId]);

export const useDeckSummary = (deckId: string, cutoff: number) =>
  useLiveQuery(
    db
      .select(summaryColumns(cutoff))
      .from(decks)
      .leftJoin(cards, eq(cards.deckId, decks.id))
      .where(eq(decks.id, deckId))
      .groupBy(decks.id)
      .limit(1),
    [deckId, cutoff],
  );
