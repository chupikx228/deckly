import { index, integer, sqliteTable, text } from 'drizzle-orm/sqlite-core';

export const DECK_SOURCE_KIND = {
  MANUAL: 'manual',
  GENERATED: 'generated',
} as const;

export type DeckSourceKind = (typeof DECK_SOURCE_KIND)[keyof typeof DECK_SOURCE_KIND];

export const decks = sqliteTable(
  'decks',
  {
    id: text('id').primaryKey(),
    title: text('title').notNull(),
    description: text('description'),
    sourceKind: text('source_kind').$type<DeckSourceKind>().notNull(),
    generationJobId: text('generation_job_id'),
    isArchived: integer('is_archived', { mode: 'boolean' }).notNull().default(false),
    createdAt: integer('created_at').notNull(),
    updatedAt: integer('updated_at').notNull(),
  },
  (table) => [index('decks_updated_at_idx').on(table.updatedAt)],
);

export type DeckRow = typeof decks.$inferSelect;
export type DeckInsert = typeof decks.$inferInsert;
