import { describe, expect, it } from 'vitest';

import { CARD_STATE, NOTE_TYPE, type CardState } from '@/shared/config';
import type { CardRow, NoteRow } from '@/shared/db';

import { buildStudyQueue, type StudyCard } from './build-queue';

const note: NoteRow = {
  id: 'note-1',
  deckId: 'deck-1',
  noteType: NOTE_TYPE.BASIC,
  fields: { front: 'a', back: 'b' },
  tags: [],
  sources: [],
  createdAt: 0,
  updatedAt: 0,
};

const makeCard = (id: string, state: CardState, due: number): StudyCard => ({
  note,
  card: {
    id,
    noteId: 'note-1',
    deckId: 'deck-1',
    templateOrdinal: 0,
    state,
    due,
    stability: 0,
    difficulty: 0,
    elapsedDays: 0,
    scheduledDays: 0,
    learningSteps: 0,
    reps: 0,
    lapses: 0,
    lastReviewedAt: null,
    isSuspended: false,
    createdAt: 0,
    updatedAt: 0,
  } satisfies CardRow,
});

const ids = (items: readonly StudyCard[]): string[] => items.map((item) => item.card.id);

const GENEROUS = { dailyReviewLimit: 100, dailyNewLimit: 100 };

describe('buildStudyQueue', () => {
  it('puts learning cards before reviews and reviews before new ones', () => {
    const queue = buildStudyQueue({
      dueCards: [
        makeCard('review-1', CARD_STATE.REVIEW, 1),
        makeCard('learning-1', CARD_STATE.LEARNING, 2),
      ],
      newCards: [makeCard('new-1', CARD_STATE.NEW, 0)],
      ...GENEROUS,
    });

    expect(ids(queue)).toEqual(['learning-1', 'review-1', 'new-1']);
  });

  it('treats relearning cards as learning', () => {
    const queue = buildStudyQueue({
      dueCards: [
        makeCard('review-1', CARD_STATE.REVIEW, 1),
        makeCard('relearning-1', CARD_STATE.RELEARNING, 2),
      ],
      newCards: [],
      ...GENEROUS,
    });

    expect(ids(queue)).toEqual(['relearning-1', 'review-1']);
  });

  it('caps reviews at the daily limit', () => {
    const queue = buildStudyQueue({
      dueCards: [
        makeCard('r1', CARD_STATE.REVIEW, 1),
        makeCard('r2', CARD_STATE.REVIEW, 2),
        makeCard('r3', CARD_STATE.REVIEW, 3),
      ],
      newCards: [],
      dailyReviewLimit: 2,
      dailyNewLimit: 100,
    });

    expect(ids(queue)).toEqual(['r1', 'r2']);
  });

  it('caps new cards at the daily limit', () => {
    const queue = buildStudyQueue({
      dueCards: [],
      newCards: [makeCard('n1', CARD_STATE.NEW, 0), makeCard('n2', CARD_STATE.NEW, 0)],
      dailyReviewLimit: 100,
      dailyNewLimit: 1,
    });

    expect(ids(queue)).toEqual(['n1']);
  });

  it('never caps learning cards, because they must finish today', () => {
    const queue = buildStudyQueue({
      dueCards: [
        makeCard('l1', CARD_STATE.LEARNING, 1),
        makeCard('l2', CARD_STATE.LEARNING, 2),
        makeCard('l3', CARD_STATE.LEARNING, 3),
      ],
      newCards: [],
      dailyReviewLimit: 1,
      dailyNewLimit: 0,
    });

    expect(ids(queue)).toEqual(['l1', 'l2', 'l3']);
  });

  it('returns an empty queue when the limits are zero', () => {
    const queue = buildStudyQueue({
      dueCards: [makeCard('r1', CARD_STATE.REVIEW, 1)],
      newCards: [makeCard('n1', CARD_STATE.NEW, 0)],
      dailyReviewLimit: 0,
      dailyNewLimit: 0,
    });

    expect(queue).toEqual([]);
  });
});
