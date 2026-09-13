import { and, eq, lte, sql } from 'drizzle-orm';
import { useLiveQuery } from 'drizzle-orm/expo-sqlite';

import { cards, db } from '@/shared/db';

export const useDueCardCount = (cutoff: number) =>
  useLiveQuery(
    db
      .select({ total: sql<number>`count(*)` })
      .from(cards)
      .where(and(eq(cards.isSuspended, false), lte(cards.due, cutoff))),
    [cutoff],
  );
