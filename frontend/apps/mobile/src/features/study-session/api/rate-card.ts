import { eq } from 'drizzle-orm';

import { scheduleCard, toCardUpdate, toReviewInsert } from '@/entities/card';
import type { Rating } from '@/shared/config';
import { cards, db, reviews } from '@/shared/db';
import type { CardRow } from '@/shared/db';

export interface RateCardInput {
  readonly card: CardRow;
  readonly rating: Rating;
  readonly now: number;
  readonly durationMs: number;
}

export const rateCard = ({ card, rating, now, durationMs }: RateCardInput): void => {
  const result = scheduleCard(card, rating, now);

  db.transaction((tx) => {
    tx.update(cards).set(toCardUpdate(result, now)).where(eq(cards.id, card.id)).run();
    tx.insert(reviews)
      .values(toReviewInsert(card.id, result, durationMs))
      .run();
  });
};
