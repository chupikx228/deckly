import { isTerminalJobStatus, type GenerationJob } from '@deckly/api-contract';
import { useCallback, useEffect, useState } from 'react';

import { GENERATION } from '@/shared/config';

import { isGenerationTimedOut } from './generation';

export interface LongWaitState {
  isLongWait: boolean;
  dismiss: () => void;
}

export const useLongWait = (job: GenerationJob | undefined): LongWaitState => {
  const [hasWaitedTooLong, setHasWaitedTooLong] = useState(false);
  const [isDismissed, setIsDismissed] = useState(false);

  const isRunning = job !== undefined && !isTerminalJobStatus(job.status);
  const hasStalled = job !== undefined && isGenerationTimedOut(job);

  useEffect(() => {
    if (!isRunning) {
      return;
    }

    const timer = setTimeout(() => {
      setHasWaitedTooLong(true);
    }, GENERATION.HARD_TIMEOUT_MS);

    return () => {
      clearTimeout(timer);
    };
  }, [isRunning]);

  const dismiss = useCallback(() => {
    setIsDismissed(true);
  }, []);

  return {
    isLongWait: isRunning && !isDismissed && (hasWaitedTooLong || hasStalled),
    dismiss,
  };
};
