import { CARD_STATE } from '@/shared/config';
import type { CardRow, NoteRow } from '@/shared/db';

export interface StudyCard {
  readonly card: CardRow;
  readonly note: NoteRow;
}

export interface BuildStudyQueueInput {
  readonly dueCards: readonly StudyCard[];
  readonly newCards: readonly StudyCard[];
  readonly dailyReviewLimit: number;
  readonly dailyNewLimit: number;
}

const isLearning = (item: StudyCard): boolean =>
  item.card.state === CARD_STATE.LEARNING || item.card.state === CARD_STATE.RELEARNING;

export const buildStudyQueue = ({
  dueCards,
  newCards,
  dailyReviewLimit,
  dailyNewLimit,
}: BuildStudyQueueInput): StudyCard[] => {
  const learning = dueCards.filter(isLearning);
  const review = dueCards
    .filter((item) => item.card.state === CARD_STATE.REVIEW)
    .slice(0, Math.max(dailyReviewLimit, 0));
  const fresh = newCards
    .filter((item) => item.card.state === CARD_STATE.NEW)
    .slice(0, Math.max(dailyNewLimit, 0));

  return [...learning, ...review, ...fresh];
};
