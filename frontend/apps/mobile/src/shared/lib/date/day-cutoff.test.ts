import { describe, expect, it } from 'vitest';

import { getDayCutoff, getDayStart } from './day-cutoff';

const at = (hour: number, minute = 0): number =>
  new Date(2026, 7, 14, hour, minute, 0, 0).getTime();

describe('getDayCutoff', () => {
  it('rolls over to the next day once the rollover hour has passed', () => {
    const cutoff = new Date(getDayCutoff(at(10)));

    expect(cutoff.getDate()).toBe(15);
    expect(cutoff.getHours()).toBe(4);
  });

  it('uses today when the time is before the rollover hour', () => {
    const cutoff = new Date(getDayCutoff(at(2)));

    expect(cutoff.getDate()).toBe(14);
    expect(cutoff.getHours()).toBe(4);
  });

  it('treats the rollover hour itself as the start of a new day', () => {
    expect(getDayCutoff(at(4))).toBe(getDayCutoff(at(10)));
    expect(getDayCutoff(at(3, 59))).toBeLessThan(getDayCutoff(at(4)));
  });

  it('honours a custom rollover hour', () => {
    expect(new Date(getDayCutoff(at(10), 12)).getHours()).toBe(12);
  });
});

describe('getDayStart', () => {
  it('sits exactly one day before the cutoff', () => {
    const now = at(10);

    expect(getDayCutoff(now) - getDayStart(now)).toBe(24 * 60 * 60 * 1000);
  });
});
