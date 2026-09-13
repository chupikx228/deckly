import { createEmptyCard, fsrs, generatorParameters } from 'ts-fsrs';
import type { FSRSParameters } from 'ts-fsrs';

import { fromFsrsCard, fromFsrsReviewLog, toFsrsCard, toFsrsGrade } from './mappers';
import {
  DEFAULT_SCHEDULER_OPTIONS,
  RATING_ORDER,
  type Rating,
  type Scheduler,
  type SchedulerCardState,
  type SchedulerOptions,
  type SchedulerPreview,
  type SchedulerResult,
} from './types';

const toFsrsParameters = (options: SchedulerOptions): Partial<FSRSParameters> => ({
  request_retention: options.requestRetention,
  maximum_interval: options.maximumInterval,
  enable_fuzz: options.enableFuzz,
  ...(options.parameters === null ? {} : { w: options.parameters }),
});

export const createFsrsScheduler = (options: Partial<SchedulerOptions> = {}): Scheduler => {
  const resolved: SchedulerOptions = { ...DEFAULT_SCHEDULER_OPTIONS, ...options };
  const instance = fsrs(generatorParameters(toFsrsParameters(resolved)));

  const schedule = (card: SchedulerCardState, rating: Rating, now: number): SchedulerResult => {
    const { card: nextCard, log } = instance.next(
      toFsrsCard(card),
      new Date(now),
      toFsrsGrade(rating),
    );

    return { card: fromFsrsCard(nextCard), log: fromFsrsReviewLog(log) };
  };

  return {
    createCard: (now: number): SchedulerCardState => fromFsrsCard(createEmptyCard(new Date(now))),

    schedule,

    preview: (card: SchedulerCardState, now: number): SchedulerPreview =>
      RATING_ORDER.reduce<Record<Rating, SchedulerResult>>(
        (accumulator, rating) => {
          accumulator[rating] = schedule(card, rating, now);

          return accumulator;
        },
        {} as Record<Rating, SchedulerResult>,
      ),

    retrievability: (card: SchedulerCardState, now: number): number =>
      instance.get_retrievability(toFsrsCard(card), new Date(now), false),

    forget: (card: SchedulerCardState, now: number): SchedulerResult => {
      const { card: nextCard, log } = instance.forget(toFsrsCard(card), new Date(now));

      return { card: fromFsrsCard(nextCard), log: fromFsrsReviewLog(log) };
    },
  };
};
