export const DEFAULT_ROLLOVER_HOUR = 4;

const DAY_MS = 24 * 60 * 60 * 1000;

export const getDayCutoff = (now: number, rolloverHour = DEFAULT_ROLLOVER_HOUR): number => {
  const date = new Date(now);
  const cutoff = new Date(now);

  cutoff.setHours(rolloverHour, 0, 0, 0);

  if (date.getHours() >= rolloverHour) {
    cutoff.setTime(cutoff.getTime() + DAY_MS);
  }

  return cutoff.getTime();
};

export const getDayStart = (now: number, rolloverHour = DEFAULT_ROLLOVER_HOUR): number =>
  getDayCutoff(now, rolloverHour) - DAY_MS;
