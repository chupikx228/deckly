import { Rating as FsrsRating, State as FsrsState } from 'ts-fsrs';
import type { Card as FsrsCard, CardInput, Grade, ReviewLog as FsrsReviewLog } from 'ts-fsrs';

import { CARD_STATE, RATING, type CardState, type Rating } from './types';
import type { SchedulerCardState, SchedulerReviewLog } from './types';

const RATING_TO_FSRS: Readonly<Record<Rating, Grade>> = {
  [RATING.AGAIN]: FsrsRating.Again,
  [RATING.HARD]: FsrsRating.Hard,
  [RATING.GOOD]: FsrsRating.Good,
  [RATING.EASY]: FsrsRating.Easy,
};

const STATE_TO_FSRS: Readonly<Record<CardState, FsrsState>> = {
  [CARD_STATE.NEW]: FsrsState.New,
  [CARD_STATE.LEARNING]: FsrsState.Learning,
  [CARD_STATE.REVIEW]: FsrsState.Review,
  [CARD_STATE.RELEARNING]: FsrsState.Relearning,
};

const STATE_FROM_FSRS: Readonly<Record<FsrsState, CardState>> = {
  [FsrsState.New]: CARD_STATE.NEW,
  [FsrsState.Learning]: CARD_STATE.LEARNING,
  [FsrsState.Review]: CARD_STATE.REVIEW,
  [FsrsState.Relearning]: CARD_STATE.RELEARNING,
};

const RATING_FROM_FSRS: Readonly<Record<FsrsRating, Rating>> = {
  [FsrsRating.Manual]: RATING.AGAIN,
  [FsrsRating.Again]: RATING.AGAIN,
  [FsrsRating.Hard]: RATING.HARD,
  [FsrsRating.Good]: RATING.GOOD,
  [FsrsRating.Easy]: RATING.EASY,
};

export const toFsrsGrade = (rating: Rating): Grade => RATING_TO_FSRS[rating];

export const toFsrsCard = (card: SchedulerCardState): CardInput => ({
  due: new Date(card.due),
  stability: card.stability,
  difficulty: card.difficulty,
  elapsed_days: card.elapsedDays,
  scheduled_days: card.scheduledDays,
  learning_steps: card.learningSteps,
  reps: card.reps,
  lapses: card.lapses,
  state: STATE_TO_FSRS[card.state],
  last_review: card.lastReviewedAt === null ? null : new Date(card.lastReviewedAt),
});

export const fromFsrsCard = (card: FsrsCard): SchedulerCardState => ({
  state: STATE_FROM_FSRS[card.state],
  due: card.due.getTime(),
  stability: card.stability,
  difficulty: card.difficulty,
  elapsedDays: card.elapsed_days,
  scheduledDays: card.scheduled_days,
  learningSteps: card.learning_steps,
  reps: card.reps,
  lapses: card.lapses,
  lastReviewedAt: card.last_review === undefined ? null : card.last_review.getTime(),
});

export const fromFsrsReviewLog = (log: FsrsReviewLog): SchedulerReviewLog => ({
  rating: RATING_FROM_FSRS[log.rating],
  state: STATE_FROM_FSRS[log.state],
  due: log.due.getTime(),
  stability: log.stability,
  difficulty: log.difficulty,
  elapsedDays: log.elapsed_days,
  lastElapsedDays: log.last_elapsed_days,
  scheduledDays: log.scheduled_days,
  reviewedAt: log.review.getTime(),
});
