const CLOZE_PATTERN = /\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}/g;

export const CLOZE_PLACEHOLDER = '[…]';

export const CLOZE_SYNTAX_HINT = '{{c1::…}}';
export const CLOZE_SYNTAX_EXAMPLE = '{{c1::1969}}';

export interface ClozeDeletion {
  readonly index: number;
  readonly answer: string;
  readonly hint: string | null;
  readonly start: number;
  readonly end: number;
}

export const parseClozeDeletions = (text: string): ClozeDeletion[] => {
  const deletions: ClozeDeletion[] = [];
  const pattern = new RegExp(CLOZE_PATTERN);

  let match = pattern.exec(text);

  while (match !== null) {
    const [full, rawIndex, answer, hint] = match;

    if (rawIndex !== undefined && answer !== undefined) {
      deletions.push({
        index: Number.parseInt(rawIndex, 10),
        answer,
        hint: hint ?? null,
        start: match.index,
        end: match.index + full.length,
      });
    }

    match = pattern.exec(text);
  }

  return deletions;
};

export const getClozeIndices = (text: string): number[] =>
  [...new Set(parseClozeDeletions(text).map((deletion) => deletion.index))].sort((a, b) => a - b);

export const CLOZE_ERROR = {
  NO_DELETIONS: 'no_deletions',
  NOT_STARTING_AT_ONE: 'not_starting_at_one',
  HAS_GAPS: 'has_gaps',
} as const;

export type ClozeError = (typeof CLOZE_ERROR)[keyof typeof CLOZE_ERROR];

export const validateClozeText = (text: string): ClozeError | null => {
  const indices = getClozeIndices(text);

  if (indices.length === 0) {
    return CLOZE_ERROR.NO_DELETIONS;
  }

  if (indices[0] !== 1) {
    return CLOZE_ERROR.NOT_STARTING_AT_ONE;
  }

  const hasGaps = indices.some((value, position) => value !== position + 1);

  return hasGaps ? CLOZE_ERROR.HAS_GAPS : null;
};

const renderCloze = (text: string, targetIndex: number, hideTarget: boolean): string => {
  const deletions = parseClozeDeletions(text);

  let result = '';
  let cursor = 0;

  for (const deletion of deletions) {
    result += text.slice(cursor, deletion.start);

    if (deletion.index !== targetIndex) {
      result += deletion.answer;
    } else if (hideTarget) {
      result += deletion.hint === null ? CLOZE_PLACEHOLDER : `[${deletion.hint}]`;
    } else {
      result += deletion.answer;
    }

    cursor = deletion.end;
  }

  return result + text.slice(cursor);
};

export const renderClozeQuestion = (text: string, targetIndex: number): string =>
  renderCloze(text, targetIndex, true);

export const renderClozeAnswer = (text: string, targetIndex: number): string =>
  renderCloze(text, targetIndex, false);
