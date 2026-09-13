const MINUTE_MS = 60 * 1000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const MONTH_MS = 30 * DAY_MS;
const YEAR_MS = 365 * DAY_MS;

export const INTERVAL_UNIT = {
  MINUTES: 'minutes',
  HOURS: 'hours',
  DAYS: 'days',
  MONTHS: 'months',
  YEARS: 'years',
} as const;

export type IntervalUnit = (typeof INTERVAL_UNIT)[keyof typeof INTERVAL_UNIT];

export interface IntervalDescriptor {
  readonly unit: IntervalUnit;
  readonly count: number;
}

export const describeInterval = (durationMs: number): IntervalDescriptor => {
  const duration = Math.max(durationMs, 0);

  if (duration < HOUR_MS) {
    return { unit: INTERVAL_UNIT.MINUTES, count: Math.max(Math.round(duration / MINUTE_MS), 1) };
  }

  if (duration < DAY_MS) {
    return { unit: INTERVAL_UNIT.HOURS, count: Math.round(duration / HOUR_MS) };
  }

  if (duration < MONTH_MS) {
    return { unit: INTERVAL_UNIT.DAYS, count: Math.round(duration / DAY_MS) };
  }

  if (duration < YEAR_MS) {
    return { unit: INTERVAL_UNIT.MONTHS, count: Math.round(duration / MONTH_MS) };
  }

  return { unit: INTERVAL_UNIT.YEARS, count: Math.round(duration / YEAR_MS) };
};
