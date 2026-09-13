import { and, asc, eq, lte, ne } from 'drizzle-orm';

import { CARD_STATE } from '@/shared/config';
import { cards, db, notes } from '@/shared/db';

import { buildStudyQueue, type StudyCard } from '../model/build-queue';

export interface LoadStudyQueueInput {
  readonly deckId: string;
  readonly cutoff: number;
  readonly dailyReviewLimit: number;
  readonly dailyNewLimit: number;
}

export const loadStudyQueue = ({
  deckId,
  cutoff,
  dailyReviewLimit,
  dailyNewLimit,
}: LoadStudyQueueInput): StudyCard[] => {
  const dueCards = db
    .select({ card: cards, note: notes })
    .from(cards)
    .innerJoin(notes, eq(cards.noteId, notes.id))
    .where(
      and(
        eq(cards.deckId, deckId),
        eq(cards.isSuspended, false),
        ne(cards.state, CARD_STATE.NEW),
        lte(cards.due, cutoff),
      ),
    )
    .orderBy(asc(cards.due))
    .all();

  const newCards = db
    .select({ card: cards, note: notes })
    .from(cards)
    .innerJoin(notes, eq(cards.noteId, notes.id))
    .where(
      and(eq(cards.deckId, deckId), eq(cards.isSuspended, false), eq(cards.state, CARD_STATE.NEW)),
    )
    .orderBy(asc(cards.createdAt), asc(cards.templateOrdinal))
    .limit(Math.max(dailyNewLimit, 0))
    .all();

  return buildStudyQueue({ dueCards, newCards, dailyReviewLimit, dailyNewLimit });
};
