import type { Rating, SchedulerCardState, SchedulerResult } from '@deckly/srs';

import type { CardInsert, CardRow, ReviewInsert } from '@/shared/db';
import { createId } from '@/shared/lib';

import { scheduler } from './scheduler';

export const toSchedulerState = (card: CardRow): SchedulerCardState => ({
  state: card.state,
  due: card.due,
  stability: card.stability,
  difficulty: card.difficulty,
  elapsedDays: card.elapsedDays,
  scheduledDays: card.scheduledDays,
  learningSteps: card.learningSteps,
  reps: card.reps,
  lapses: card.lapses,
  lastReviewedAt: card.lastReviewedAt,
});

export const toCardUpdate = (result: SchedulerResult, now: number): Partial<CardInsert> => ({
  state: result.card.state,
  due: result.card.due,
  stability: result.card.stability,
  difficulty: result.card.difficulty,
  elapsedDays: result.card.elapsedDays,
  scheduledDays: result.card.scheduledDays,
  learningSteps: result.card.learningSteps,
  reps: result.card.reps,
  lapses: result.card.lapses,
  lastReviewedAt: result.card.lastReviewedAt,
  updatedAt: now,
});

export const toReviewInsert = (
  cardId: string,
  result: SchedulerResult,
  durationMs: number,
): ReviewInsert => ({
  id: createId(),
  cardId,
  rating: result.log.rating,
  state: result.log.state,
  due: result.log.due,
  stability: result.log.stability,
  difficulty: result.log.difficulty,
  elapsedDays: result.log.elapsedDays,
  lastElapsedDays: result.log.lastElapsedDays,
  scheduledDays: result.log.scheduledDays,
  reviewedAt: result.log.reviewedAt,
  durationMs,
});

export const previewCard = (card: CardRow, now: number): Readonly<Record<Rating, number>> => {
  const preview = scheduler.preview(toSchedulerState(card), now);

  return {
    1: preview[1].card.due,
    2: preview[2].card.due,
    3: preview[3].card.due,
    4: preview[4].card.due,
  };
};

export const scheduleCard = (card: CardRow, rating: Rating, now: number): SchedulerResult =>
  scheduler.schedule(toSchedulerState(card), rating, now);
