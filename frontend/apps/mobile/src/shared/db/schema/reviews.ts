import type { CardState, Rating } from '@deckly/srs';
import { index, integer, real, sqliteTable, text } from 'drizzle-orm/sqlite-core';

import { cards } from './cards';

export const reviews = sqliteTable(
  'reviews',
  {
    id: text('id').primaryKey(),
    cardId: text('card_id')
      .notNull()
      .references(() => cards.id, { onDelete: 'cascade' }),
    rating: integer('rating').$type<Rating>().notNull(),
    state: text('state').$type<CardState>().notNull(),
    due: integer('due').notNull(),
    stability: real('stability').notNull(),
    difficulty: real('difficulty').notNull(),
    elapsedDays: real('elapsed_days').notNull(),
    lastElapsedDays: real('last_elapsed_days').notNull(),
    scheduledDays: real('scheduled_days').notNull(),
    reviewedAt: integer('reviewed_at').notNull(),
    durationMs: integer('duration_ms').notNull(),
  },
  (table) => [
    index('reviews_card_id_idx').on(table.cardId),
    index('reviews_reviewed_at_idx').on(table.reviewedAt),
  ],
);

export type ReviewRow = typeof reviews.$inferSelect;
export type ReviewInsert = typeof reviews.$inferInsert;
