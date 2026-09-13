import { describe, expect, it } from 'vitest';

import { NOTE_TYPE } from '@/shared/config';

import {
  FORWARD_ORDINAL,
  REVERSE_ORDINAL,
  deriveCardOrdinals,
  getCardFaces,
} from './card-templates';
import { parseTypedNote } from './note-fields';

const basic = parseTypedNote(NOTE_TYPE.BASIC, { front: 'dog', back: 'собака' });
const reversed = parseTypedNote(NOTE_TYPE.BASIC_REVERSED, { front: 'dog', back: 'собака' });
const cloze = parseTypedNote(NOTE_TYPE.CLOZE, {
  text: '{{c1::Paris}} is the capital of {{c2::France}}.',
});

describe('deriveCardOrdinals', () => {
  it('produces one card for a basic note', () => {
    expect(deriveCardOrdinals(basic)).toEqual([FORWARD_ORDINAL]);
  });

  it('produces two cards for a reversed note', () => {
    expect(deriveCardOrdinals(reversed)).toEqual([FORWARD_ORDINAL, REVERSE_ORDINAL]);
  });

  it('produces one card per cloze index', () => {
    expect(deriveCardOrdinals(cloze)).toEqual([1, 2]);
  });

  it('adds the reverse card only when the extra field is filled', () => {
    const without = parseTypedNote(NOTE_TYPE.BASIC_OPTIONAL_REVERSED, {
      front: 'dog',
      back: 'собака',
    });
    const with_ = parseTypedNote(NOTE_TYPE.BASIC_OPTIONAL_REVERSED, {
      front: 'dog',
      back: 'собака',
      addReverse: 'y',
    });

    expect(deriveCardOrdinals(without)).toEqual([FORWARD_ORDINAL]);
    expect(deriveCardOrdinals(with_)).toEqual([FORWARD_ORDINAL, REVERSE_ORDINAL]);
  });

  it('produces one card per occlusion region', () => {
    const note = parseTypedNote(NOTE_TYPE.IMAGE_OCCLUSION, {
      imageId: 'img-1',
      regions: [
        { ordinal: 1, x: 0.1, y: 0.1, width: 0.2, height: 0.2 },
        { ordinal: 2, x: 0.4, y: 0.4, width: 0.2, height: 0.2 },
      ],
    });

    expect(deriveCardOrdinals(note)).toEqual([1, 2]);
  });
});

describe('getCardFaces', () => {
  it('shows front then back on the forward card', () => {
    expect(getCardFaces(basic, FORWARD_ORDINAL)).toEqual({
      question: 'dog',
      answer: 'собака',
      hint: null,
    });
  });

  it('swaps the faces on the reverse card', () => {
    expect(getCardFaces(reversed, REVERSE_ORDINAL)).toEqual({
      question: 'собака',
      answer: 'dog',
      hint: null,
    });
  });

  it('hides only the targeted cloze deletion', () => {
    const faces = getCardFaces(cloze, 2);

    expect(faces.question).toContain('Paris');
    expect(faces.question).not.toContain('France');
    expect(faces.answer).toContain('France');
  });

  it('exposes the extra field as a hint', () => {
    const note = parseTypedNote(NOTE_TYPE.CLOZE, {
      text: '{{c1::1969}}',
      extra: 'Apollo 11',
    });

    expect(getCardFaces(note, 1).hint).toBe('Apollo 11');
  });

  it('returns no hint when the extra field is blank', () => {
    expect(getCardFaces(cloze, 1).hint).toBeNull();
  });
});
