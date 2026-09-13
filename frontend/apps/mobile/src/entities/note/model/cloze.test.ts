import { describe, expect, it } from 'vitest';

import {
  CLOZE_ERROR,
  CLOZE_PLACEHOLDER,
  getClozeIndices,
  parseClozeDeletions,
  renderClozeAnswer,
  renderClozeQuestion,
  validateClozeText,
} from './cloze';

const MOON = 'Humans landed on the moon in {{c1::1969}}.';
const CAPITALS = '{{c1::Paris}} is the capital of {{c2::France}}.';
const WITH_HINT = 'The powerhouse of the cell is the {{c1::mitochondrion::organelle}}.';

describe('parseClozeDeletions', () => {
  it('extracts a single deletion', () => {
    const [deletion] = parseClozeDeletions(MOON);

    expect(deletion?.index).toBe(1);
    expect(deletion?.answer).toBe('1969');
    expect(deletion?.hint).toBeNull();
  });

  it('extracts a hint when present', () => {
    const [deletion] = parseClozeDeletions(WITH_HINT);

    expect(deletion?.answer).toBe('mitochondrion');
    expect(deletion?.hint).toBe('organelle');
  });

  it('returns nothing for plain text', () => {
    expect(parseClozeDeletions('No deletions here')).toEqual([]);
  });
});

describe('getClozeIndices', () => {
  it('returns sorted unique indices', () => {
    expect(getClozeIndices('{{c2::b}} {{c1::a}} {{c2::c}}')).toEqual([1, 2]);
  });

  it('returns an empty list for plain text', () => {
    expect(getClozeIndices('nothing')).toEqual([]);
  });
});

describe('renderClozeQuestion', () => {
  it('hides only the target deletion', () => {
    expect(renderClozeQuestion(CAPITALS, 1)).toBe(`${CLOZE_PLACEHOLDER} is the capital of France.`);
    expect(renderClozeQuestion(CAPITALS, 2)).toBe(`Paris is the capital of ${CLOZE_PLACEHOLDER}.`);
  });

  it('shows the hint instead of the placeholder', () => {
    expect(renderClozeQuestion(WITH_HINT, 1)).toBe(
      'The powerhouse of the cell is the [organelle].',
    );
  });

  it('hides every deletion sharing the target index', () => {
    expect(renderClozeQuestion('{{c1::a}} and {{c1::b}}', 1)).toBe(
      `${CLOZE_PLACEHOLDER} and ${CLOZE_PLACEHOLDER}`,
    );
  });
});

describe('renderClozeAnswer', () => {
  it('reveals every deletion', () => {
    expect(renderClozeAnswer(CAPITALS, 1)).toBe('Paris is the capital of France.');
    expect(renderClozeAnswer(WITH_HINT, 1)).toBe(
      'The powerhouse of the cell is the mitochondrion.',
    );
  });
});

describe('validateClozeText', () => {
  it('accepts a well-formed text', () => {
    expect(validateClozeText(CAPITALS)).toBeNull();
  });

  it('rejects text without deletions', () => {
    expect(validateClozeText('plain text')).toBe(CLOZE_ERROR.NO_DELETIONS);
  });

  it('rejects numbering that does not start at one', () => {
    expect(validateClozeText('{{c2::a}}')).toBe(CLOZE_ERROR.NOT_STARTING_AT_ONE);
  });

  it('rejects gaps in the numbering', () => {
    expect(validateClozeText('{{c1::a}} {{c3::b}}')).toBe(CLOZE_ERROR.HAS_GAPS);
  });
});
