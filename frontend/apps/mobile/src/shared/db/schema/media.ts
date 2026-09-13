import type { MediaKind } from '@deckly/api-contract';
import { index, integer, sqliteTable, text } from 'drizzle-orm/sqlite-core';

import { notes } from './notes';

export const media = sqliteTable(
  'media',
  {
    id: text('id').primaryKey(),
    noteId: text('note_id')
      .notNull()
      .references(() => notes.id, { onDelete: 'cascade' }),
    kind: text('kind').$type<MediaKind>().notNull(),
    localUri: text('local_uri'),
    remoteUrl: text('remote_url'),
    alt: text('alt'),
    width: integer('width'),
    height: integer('height'),
    license: text('license'),
    createdAt: integer('created_at').notNull(),
    updatedAt: integer('updated_at').notNull(),
  },
  (table) => [index('media_note_id_idx').on(table.noteId)],
);

export type MediaRow = typeof media.$inferSelect;
export type MediaInsert = typeof media.$inferInsert;
