import { DOCUMENT_UNIT } from '@deckly/api-contract';
import { describe, expect, it } from 'vitest';

import enGeneration from '@/shared/i18n/locales/en/generation.json';
import ruGeneration from '@/shared/i18n/locales/ru/generation.json';

import { DOCUMENT_UNIT_CITATION_KEY, formatTimestamp } from './source-citation';

const CITATION_KEYS = [
  ...Object.values(DOCUMENT_UNIT_CITATION_KEY),
  'source.web',
  'source.transcript',
];

const resolveKey = (bundle: Record<string, unknown>, key: string): unknown =>
  key.split('.').reduce<unknown>((value, segment) => {
    if (typeof value !== 'object' || value === null) {
      return undefined;
    }

    return Object.getOwnPropertyDescriptor(value, segment)?.value;
  }, bundle);

describe('formatTimestamp', () => {
  it('renders the start of a recording as zero minutes', () => {
    expect(formatTimestamp(0)).toBe('0:00');
  });

  it('pads seconds below ten', () => {
    expect(formatTimestamp(62_480)).toBe('1:02');
  });

  it('truncates rather than rounds a partial second', () => {
    expect(formatTimestamp(59_999)).toBe('0:59');
  });

  it('rolls over to the next minute exactly on the boundary', () => {
    expect(formatTimestamp(60_000)).toBe('1:00');
  });

  it('keeps counting minutes past an hour instead of wrapping', () => {
    expect(formatTimestamp(3_600_000)).toBe('60:00');
  });
});

describe('DOCUMENT_UNIT_CITATION_KEY', () => {
  it('gives a page and a slide their own citation copy', () => {
    expect(DOCUMENT_UNIT_CITATION_KEY[DOCUMENT_UNIT.PAGE]).not.toBe(
      DOCUMENT_UNIT_CITATION_KEY[DOCUMENT_UNIT.SLIDE],
    );
  });
});

describe('citation copy', () => {
  it.each([
    ['en', enGeneration],
    ['ru', ruGeneration],
  ])('resolves every citation key against the %s bundle', (_locale, bundle) => {
    for (const key of CITATION_KEYS) {
      expect(typeof resolveKey(bundle, key)).toBe('string');
    }
  });

  it.each([
    ['en', enGeneration],
    ['ru', ruGeneration],
  ])('interpolates the title into every %s citation', (_locale, bundle) => {
    for (const key of CITATION_KEYS) {
      expect(resolveKey(bundle, key)).toContain('{{title}}');
    }
  });
});
