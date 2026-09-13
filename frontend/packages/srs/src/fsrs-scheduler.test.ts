import { describe, expect, it } from 'vitest';

import { createFsrsScheduler } from './fsrs-scheduler';
import { CARD_STATE, RATING, type SchedulerCardState } from './types';

const NOW = Date.UTC(2026, 0, 1, 12, 0, 0);
const DAY_MS = 24 * 60 * 60 * 1000;

const createScheduler = () => createFsrsScheduler({ enableFuzz: false });

const reviewCardAfter = (days: number): SchedulerCardState => {
  const scheduler = createScheduler();
  let card = scheduler.createCard(NOW);
  let at = NOW;

  for (let index = 0; index < 3; index += 1) {
    card = scheduler.schedule(card, RATING.GOOD, at).card;
    at = card.due;
  }

  return scheduler.schedule(card, RATING.GOOD, at + days * DAY_MS).card;
};

describe('createFsrsScheduler', () => {
  it('creates a new card that is due immediately', () => {
    const card = createScheduler().createCard(NOW);

    expect(card.state).toBe(CARD_STATE.NEW);
    expect(card.due).toBe(NOW);
    expect(card.reps).toBe(0);
    expect(card.lapses).toBe(0);
    expect(card.lastReviewedAt).toBeNull();
  });

  it('moves a new card out of the new state on the first rating', () => {
    const scheduler = createScheduler();
    const { card } = scheduler.schedule(scheduler.createCard(NOW), RATING.GOOD, NOW);

    expect(card.state).not.toBe(CARD_STATE.NEW);
    expect(card.reps).toBe(1);
    expect(card.lastReviewedAt).toBe(NOW);
    expect(card.due).toBeGreaterThan(NOW);
  });

  it('orders preview intervals from again to easy', () => {
    const scheduler = createScheduler();
    const card = reviewCardAfter(10);
    const preview = scheduler.preview(card, card.due);

    expect(preview[RATING.AGAIN].card.due).toBeLessThanOrEqual(preview[RATING.HARD].card.due);
    expect(preview[RATING.HARD].card.due).toBeLessThanOrEqual(preview[RATING.GOOD].card.due);
    expect(preview[RATING.GOOD].card.due).toBeLessThanOrEqual(preview[RATING.EASY].card.due);
  });

  it('does not mutate the card passed to preview', () => {
    const scheduler = createScheduler();
    const card = reviewCardAfter(10);
    const snapshot = { ...card };

    scheduler.preview(card, card.due);

    expect(card).toEqual(snapshot);
  });

  it('counts a lapse when a review card is forgotten', () => {
    const scheduler = createScheduler();
    const card = reviewCardAfter(10);
    const { card: lapsed } = scheduler.schedule(card, RATING.AGAIN, card.due);

    expect(lapsed.state).toBe(CARD_STATE.RELEARNING);
    expect(lapsed.lapses).toBe(card.lapses + 1);
  });

  it('writes a review log holding the pre-review memory state', () => {
    const scheduler = createScheduler();
    const card = reviewCardAfter(10);
    const reviewedAt = card.due + DAY_MS;
    const { card: next, log } = scheduler.schedule(card, RATING.HARD, reviewedAt);

    expect(log.rating).toBe(RATING.HARD);
    expect(log.state).toBe(card.state);
    expect(log.reviewedAt).toBe(reviewedAt);
    expect(log.stability).toBe(card.stability);
    expect(log.difficulty).toBe(card.difficulty);
    expect(next.stability).not.toBe(card.stability);
  });

  it('decays retrievability as time passes', () => {
    const scheduler = createScheduler();
    const card = reviewCardAfter(10);

    const onReview = scheduler.retrievability(card, card.lastReviewedAt ?? NOW);
    const muchLater = scheduler.retrievability(card, card.due + 365 * DAY_MS);

    expect(onReview).toBeGreaterThan(muchLater);
    expect(muchLater).toBeGreaterThanOrEqual(0);
    expect(onReview).toBeLessThanOrEqual(1);
  });

  it('resets a card to the new state when it is forgotten', () => {
    const scheduler = createScheduler();
    const card = reviewCardAfter(10);
    const { card: forgotten } = scheduler.forget(card, card.due);

    expect(forgotten.state).toBe(CARD_STATE.NEW);
    expect(forgotten.stability).toBe(0);
    expect(forgotten.difficulty).toBe(0);
  });

  it('is deterministic when fuzz is disabled', () => {
    const card = createScheduler().createCard(NOW);

    const first = createScheduler().schedule(card, RATING.GOOD, NOW).card;
    const second = createScheduler().schedule(card, RATING.GOOD, NOW).card;

    expect(first).toEqual(second);
  });

  it('schedules a shorter interval at a higher retention target', () => {
    const card = reviewCardAfter(10);

    const relaxed = createFsrsScheduler({ enableFuzz: false, requestRetention: 0.8 }).schedule(
      card,
      RATING.GOOD,
      card.due,
    ).card.due;
    const strict = createFsrsScheduler({ enableFuzz: false, requestRetention: 0.95 }).schedule(
      card,
      RATING.GOOD,
      card.due,
    ).card.due;

    expect(strict).toBeLessThan(relaxed);
  });
});
