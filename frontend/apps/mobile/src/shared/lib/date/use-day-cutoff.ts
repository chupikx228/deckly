import { useSyncExternalStore } from 'react';

import { getDayCutoff } from './day-cutoff';

const listeners = new Set<() => void>();

let cutoff = getDayCutoff(Date.now());
let timer: ReturnType<typeof setTimeout> | null = null;

const notify = (): void => {
  for (const listener of listeners) {
    listener();
  }
};

const scheduleRollover = (): void => {
  if (timer !== null) {
    clearTimeout(timer);
  }

  timer = setTimeout(
    () => {
      cutoff = getDayCutoff(Date.now());
      notify();
      scheduleRollover();
    },
    Math.max(cutoff - Date.now(), 1_000),
  );
};

const subscribe = (listener: () => void): (() => void) => {
  listeners.add(listener);

  if (listeners.size === 1) {
    cutoff = getDayCutoff(Date.now());
    scheduleRollover();
  }

  return () => {
    listeners.delete(listener);

    if (listeners.size === 0 && timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };
};

const getSnapshot = (): number => cutoff;

export const useDayCutoff = (): number => useSyncExternalStore(subscribe, getSnapshot);
