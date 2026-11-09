import { DOCUMENT_UNIT, type DocumentUnit } from '@deckly/api-contract';

const MS_PER_SECOND = 1_000;
const SECONDS_PER_MINUTE = 60;
const SECONDS_WIDTH = 2;

export const DOCUMENT_UNIT_CITATION_KEY = {
  [DOCUMENT_UNIT.PAGE]: 'source.documentPage',
  [DOCUMENT_UNIT.SLIDE]: 'source.documentSlide',
} as const satisfies Record<DocumentUnit, string>;

export const formatTimestamp = (ms: number): string => {
  const totalSeconds = Math.floor(ms / MS_PER_SECOND);
  const minutes = Math.floor(totalSeconds / SECONDS_PER_MINUTE);
  const seconds = totalSeconds % SECONDS_PER_MINUTE;

  return `${minutes}:${String(seconds).padStart(SECONDS_WIDTH, '0')}`;
};
