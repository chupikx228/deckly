export const API = {
  BASE_URL: process.env.EXPO_PUBLIC_API_URL ?? 'http://localhost:8000/v1',
  TIMEOUT_MS: 15_000,
  USE_MOCK: process.env.EXPO_PUBLIC_API_MOCK !== 'false',
} as const;

export const GENERATION = {
  POLL_INTERVAL_MS: 2_000,
  TIMEOUT_MS: 5 * 60 * 1_000,
  DEFAULT_CARD_COUNT: 30,
  MIN_CARD_COUNT: 5,
  MAX_CARD_COUNT: 200,
} as const;

export const QUERY_DEFAULTS = {
  STALE_TIME_MS: 60_000,
  RETRY_COUNT: 2,
  MAX_RETRY_DELAY_MS: 30_000,
} as const;

export const STUDY_DEFAULTS = {
  DAILY_NEW_LIMIT: 20,
  DAILY_REVIEW_LIMIT: 200,
  TARGET_RETENTION: 0.9,
} as const;
