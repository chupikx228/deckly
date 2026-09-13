import type { CardState } from '@deckly/srs';
import { index, integer, real, sqliteTable, text, unique } from 'drizzle-orm/sqlite-core';

import { decks } from './decks';
import { notes } from './notes';

export const cards = sqliteTable(
  'cards',
  {
    id: text('id').primaryKey(),
    noteId: text('note_id')
      .notNull()
      .references(() => notes.id, { onDelete: 'cascade' }),
    deckId: text('deck_id')
      .notNull()
      .references(() => decks.id, { onDelete: 'cascade' }),
    templateOrdinal: integer('template_ordinal').notNull(),
    state: text('state').$type<CardState>().notNull(),
    due: integer('due').notNull(),
    stability: real('stability').notNull(),
    difficulty: real('difficulty').notNull(),
    elapsedDays: real('elapsed_days').notNull(),
    scheduledDays: real('scheduled_days').notNull(),
    learningSteps: integer('learning_steps').notNull(),
    reps: integer('reps').notNull(),
    lapses: integer('lapses').notNull(),
    lastReviewedAt: integer('last_reviewed_at'),
    isSuspended: integer('is_suspended', { mode: 'boolean' }).notNull().default(false),
    createdAt: integer('created_at').notNull(),
    updatedAt: integer('updated_at').notNull(),
  },
  (table) => [
    index('cards_due_idx').on(table.deckId, table.isSuspended, table.due),
    index('cards_note_id_idx').on(table.noteId),
    unique('cards_note_template_unq').on(table.noteId, table.templateOrdinal),
  ],
);

export type CardRow = typeof cards.$inferSelect;
export type CardInsert = typeof cards.$inferInsert;
