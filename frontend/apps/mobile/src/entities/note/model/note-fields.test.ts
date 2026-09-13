import { describe, expect, it } from 'vitest';

import { NOTE_TYPE } from '@/shared/config';

import { parseTypedNote, safeParseTypedNote } from './note-fields';

describe('parseTypedNote', () => {
  it('trims whitespace around basic fields', () => {
    const note = parseTypedNote(NOTE_TYPE.BASIC, { front: '  dog  ', back: 'собака' });

    expect(note.fields).toEqual({ front: 'dog', back: 'собака' });
  });

  it('defaults the cloze extra field to an empty string', () => {
    const note = parseTypedNote(NOTE_TYPE.CLOZE, { text: '{{c1::a}}' });

    expect(note.noteType).toBe(NOTE_TYPE.CLOZE);
    expect(note.fields).toEqual({ text: '{{c1::a}}', extra: '' });
  });

  it('rejects a blank required field', () => {
    expect(() => parseTypedNote(NOTE_TYPE.BASIC, { front: '   ', back: 'x' })).toThrow();
  });

  it('rejects a multiple choice note with too few distractors', () => {
    expect(() =>
      parseTypedNote(NOTE_TYPE.MULTIPLE_CHOICE, {
        question: 'q',
        answer: 'a',
        distractors: ['b'],
      }),
    ).toThrow();
  });

  it('rejects occlusion regions outside the normalised range', () => {
    expect(() =>
      parseTypedNote(NOTE_TYPE.IMAGE_OCCLUSION, {
        imageId: 'img-1',
        regions: [{ ordinal: 1, x: 1.4, y: 0, width: 0.1, height: 0.1 }],
      }),
    ).toThrow();
  });
});

describe('safeParseTypedNote', () => {
  it('returns null instead of throwing on invalid input', () => {
    expect(safeParseTypedNote(NOTE_TYPE.BASIC, { front: 'only front' })).toBeNull();
  });

  it('returns the parsed note on valid input', () => {
    expect(safeParseTypedNote(NOTE_TYPE.BASIC, { front: 'a', back: 'b' })).toEqual({
      noteType: NOTE_TYPE.BASIC,
      fields: { front: 'a', back: 'b' },
    });
  });
});
