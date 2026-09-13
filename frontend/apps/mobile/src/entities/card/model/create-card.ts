import type { CardInsert } from '@/shared/db';
import { createId } from '@/shared/lib';

import { scheduler } from './scheduler';

export interface NewCardInput {
  readonly noteId: string;
  readonly deckId: string;
  readonly templateOrdinal: number;
  readonly now: number;
}

export const createNewCard = ({
  noteId,
  deckId,
  templateOrdinal,
  now,
}: NewCardInput): CardInsert => {
  const state = scheduler.createCard(now);

  return {
    id: createId(),
    noteId,
    deckId,
    templateOrdinal,
    state: state.state,
    due: state.due,
    stability: state.stability,
    difficulty: state.difficulty,
    elapsedDays: state.elapsedDays,
    scheduledDays: state.scheduledDays,
    learningSteps: state.learningSteps,
    reps: state.reps,
    lapses: state.lapses,
    lastReviewedAt: state.lastReviewedAt,
    isSuspended: false,
    createdAt: now,
    updatedAt: now,
  };
};
