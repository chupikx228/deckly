import { REJECTION_REASON, type RejectionReason } from '@deckly/api-contract';

export const REJECTION_REASON_OPTIONS = [
  REJECTION_REASON.INCORRECT,
  REJECTION_REASON.TOO_HARD,
  REJECTION_REASON.TOO_EASY,
  REJECTION_REASON.OFF_TOPIC,
  REJECTION_REASON.DUPLICATE,
  REJECTION_REASON.OTHER,
] as const;

export const REJECTION_REASON_LABEL_KEY = {
  [REJECTION_REASON.TOO_EASY]: 'rejection.too_easy',
  [REJECTION_REASON.TOO_HARD]: 'rejection.too_hard',
  [REJECTION_REASON.INCORRECT]: 'rejection.incorrect',
  [REJECTION_REASON.DUPLICATE]: 'rejection.duplicate',
  [REJECTION_REASON.OFF_TOPIC]: 'rejection.off_topic',
  [REJECTION_REASON.OTHER]: 'rejection.other',
} as const satisfies Record<RejectionReason, string>;
