export const RATING = {
  AGAIN: 1,
  HARD: 2,
  GOOD: 3,
  EASY: 4,
} as const;

export type Rating = (typeof RATING)[keyof typeof RATING];

export const RATING_ORDER = [RATING.AGAIN, RATING.HARD, RATING.GOOD, RATING.EASY] as const;

export const CARD_STATE = {
  NEW: 'new',
  LEARNING: 'learning',
  REVIEW: 'review',
  RELEARNING: 'relearning',
} as const;

export type CardState = (typeof CARD_STATE)[keyof typeof CARD_STATE];

export interface SchedulerCardState {
  readonly state: CardState;
  readonly due: number;
  readonly stability: number;
  readonly difficulty: number;
  readonly elapsedDays: number;
  readonly scheduledDays: number;
  readonly learningSteps: number;
  readonly reps: number;
  readonly lapses: number;
  readonly lastReviewedAt: number | null;
}

export interface SchedulerReviewLog {
  readonly rating: Rating;
  readonly state: CardState;
  readonly due: number;
  readonly stability: number;
  readonly difficulty: number;
  readonly elapsedDays: number;
  readonly lastElapsedDays: number;
  readonly scheduledDays: number;
  readonly reviewedAt: number;
}

export interface SchedulerResult {
  readonly card: SchedulerCardState;
  readonly log: SchedulerReviewLog;
}

export type SchedulerPreview = Readonly<Record<Rating, SchedulerResult>>;

export interface SchedulerOptions {
  readonly requestRetention: number;
  readonly maximumInterval: number;
  readonly enableFuzz: boolean;
  readonly parameters: readonly number[] | null;
}

export interface Scheduler {
  createCard(now: number): SchedulerCardState;
  schedule(card: SchedulerCardState, rating: Rating, now: number): SchedulerResult;
  preview(card: SchedulerCardState, now: number): SchedulerPreview;
  retrievability(card: SchedulerCardState, now: number): number;
  forget(card: SchedulerCardState, now: number): SchedulerResult;
}

export const DEFAULT_SCHEDULER_OPTIONS: SchedulerOptions = {
  requestRetention: 0.9,
  maximumInterval: 36500,
  enableFuzz: true,
  parameters: null,
};
