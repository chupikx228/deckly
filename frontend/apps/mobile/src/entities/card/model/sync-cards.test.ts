import { describe, expect, it } from 'vitest';

import { diffCardOrdinals } from './sync-cards';

describe('diffCardOrdinals', () => {
  it('creates every ordinal for a brand new note', () => {
    expect(diffCardOrdinals([], [0, 1])).toEqual({ toCreate: [0, 1], toDelete: [], kept: [] });
  });

  it('keeps untouched ordinals so their scheduling survives an edit', () => {
    expect(diffCardOrdinals([1, 2], [1, 2])).toEqual({
      toCreate: [],
      toDelete: [],
      kept: [1, 2],
    });
  });

  it('adds a card when a cloze deletion is added', () => {
    expect(diffCardOrdinals([1, 2], [1, 2, 3])).toEqual({
      toCreate: [3],
      toDelete: [],
      kept: [1, 2],
    });
  });

  it('removes a card when a cloze deletion is removed', () => {
    expect(diffCardOrdinals([1, 2, 3], [1, 2])).toEqual({
      toCreate: [],
      toDelete: [3],
      kept: [1, 2],
    });
  });

  it('handles switching a note from reversed to one-directional', () => {
    expect(diffCardOrdinals([0, 1], [0])).toEqual({ toCreate: [], toDelete: [1], kept: [0] });
  });
});
