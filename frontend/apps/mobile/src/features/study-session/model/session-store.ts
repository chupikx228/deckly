import { create } from 'zustand';

import { RATING, RATING_ORDER, type Rating } from '@/shared/config';

import type { StudyCard } from './build-queue';

export type RatingTally = Record<Rating, number>;

const emptyTally = (): RatingTally =>
  RATING_ORDER.reduce<RatingTally>(
    (tally, rating) => ({ ...tally, [rating]: 0 }),
    {} as RatingTally,
  );

interface SessionState {
  readonly deckId: string | null;
  readonly queue: readonly StudyCard[];
  readonly index: number;
  readonly isRevealed: boolean;
  readonly startedAt: number | null;
  readonly finishedAt: number | null;
  readonly shownAt: number | null;
  readonly tally: RatingTally;
  start: (deckId: string, queue: readonly StudyCard[], now: number) => void;
  reveal: (now: number) => void;
  advance: (rating: Rating, now: number) => void;
  reset: () => void;
}

const initialState = {
  deckId: null,
  queue: [],
  index: 0,
  isRevealed: false,
  startedAt: null,
  finishedAt: null,
  shownAt: null,
  tally: emptyTally(),
} as const;

export const useSessionStore = create<SessionState>()((set, get) => ({
  ...initialState,
  tally: emptyTally(),

  start: (deckId, queue, now) => {
    set({
      deckId,
      queue,
      index: 0,
      isRevealed: false,
      startedAt: now,
      finishedAt: null,
      shownAt: now,
      tally: emptyTally(),
    });
  },

  reveal: () => {
    set({ isRevealed: true });
  },

  advance: (rating, now) => {
    const { index, queue, tally } = get();
    const nextIndex = index + 1;
    const isLast = nextIndex >= queue.length;

    set({
      index: nextIndex,
      isRevealed: false,
      shownAt: now,
      tally: { ...tally, [rating]: tally[rating] + 1 },
      finishedAt: isLast ? now : null,
    });
  },

  reset: () => {
    set({ ...initialState, tally: emptyTally() });
  },
}));

export const selectCurrentCard = (state: SessionState): StudyCard | undefined =>
  state.queue[state.index];

export const selectTotal = (state: SessionState): number => state.queue.length;

export const selectPosition = (state: SessionState): number =>
  Math.min(state.index + 1, state.queue.length);

export const selectReviewedCount = (state: SessionState): number =>
  RATING_ORDER.reduce((total, rating) => total + state.tally[rating], 0);

export const selectAccuracy = (state: SessionState): number | null => {
  const reviewed = selectReviewedCount(state);

  return reviewed === 0 ? null : (reviewed - state.tally[RATING.AGAIN]) / reviewed;
};

export const selectDurationMs = (state: SessionState): number =>
  state.startedAt === null || state.finishedAt === null ? 0 : state.finishedAt - state.startedAt;
