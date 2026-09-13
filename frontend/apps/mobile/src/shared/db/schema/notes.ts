import type { NoteType, Source } from '@deckly/api-contract';
import { index, integer, sqliteTable, text } from 'drizzle-orm/sqlite-core';

import { decks } from './decks';

export const notes = sqliteTable(
  'notes',
  {
    id: text('id').primaryKey(),
    deckId: text('deck_id')
      .notNull()
      .references(() => decks.id, { onDelete: 'cascade' }),
    noteType: text('note_type').$type<NoteType>().notNull(),
    fields: text('fields', { mode: 'json' }).$type<Record<string, unknown>>().notNull(),
    tags: text('tags', { mode: 'json' }).$type<string[]>().notNull().default([]),
    sources: text('sources', { mode: 'json' }).$type<Source[]>().notNull().default([]),
    createdAt: integer('created_at').notNull(),
    updatedAt: integer('updated_at').notNull(),
  },
  (table) => [index('notes_deck_id_idx').on(table.deckId)],
);

export type NoteRow = typeof notes.$inferSelect;
export type NoteInsert = typeof notes.$inferInsert;
