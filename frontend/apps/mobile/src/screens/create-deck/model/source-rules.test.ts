import { describe, expect, it } from 'vitest';

import { ARTICLE_URL_MAX_LENGTH } from '@deckly/api-contract';

import type { DocumentUpload } from '@/shared/api';
import { DIFFICULTY, LOCALE, NOTE_TYPE } from '@/shared/config';

import { hasUsableSource, isLikelyArticleUrl } from './source-rules';
import type { WizardFields } from './use-wizard-fields';
import { GENERATION_SOURCE, type GeneratableType } from './wizard-options';

const noop = (): void => undefined;

const makeFields = (overrides: Partial<WizardFields> = {}): WizardFields => ({
  source: GENERATION_SOURCE.TOPIC,
  topic: '',
  cardCount: '30',
  difficulty: DIFFICULTY.INTERMEDIATE,
  noteTypes: [NOTE_TYPE.BASIC] as readonly GeneratableType[],
  contentLanguage: LOCALE.EN,
  instructions: '',
  articleUrl: '',
  setSource: noop,
  setTopic: noop,
  setCardCount: noop,
  setDifficulty: noop,
  setContentLanguage: noop,
  setInstructions: noop,
  setArticleUrl: noop,
  toggleNoteType: noop,
  ...overrides,
});

const file: DocumentUpload = {
  uri: 'file:///tmp/lecture.pdf',
  name: 'lecture.pdf',
  mimeType: 'application/pdf',
  sizeBytes: 182_400,
};

describe('isLikelyArticleUrl', () => {
  it('accepts an ordinary article link', () => {
    expect(isLikelyArticleUrl('https://en.wikipedia.org/wiki/Spaced_repetition')).toBe(true);
  });

  it('accepts a bare host, which the server upgrades to https', () => {
    expect(isLikelyArticleUrl('http://example.com')).toBe(true);
  });

  it('ignores surrounding whitespace, as the server does', () => {
    expect(isLikelyArticleUrl('  https://example.com/a  ')).toBe(true);
  });

  it('rejects a link with no scheme', () => {
    expect(isLikelyArticleUrl('example.com/article')).toBe(false);
  });

  it('rejects a scheme the endpoint does not fetch', () => {
    expect(isLikelyArticleUrl('ftp://example.com/a')).toBe(false);
  });

  it('rejects a host with no dot, which the server refuses as non-public', () => {
    expect(isLikelyArticleUrl('https://localhost')).toBe(false);
  });

  it('rejects credentials in the host, which the server refuses', () => {
    expect(isLikelyArticleUrl('https://user:pass@example.com')).toBe(false);
  });

  it('rejects a space inside the link', () => {
    expect(isLikelyArticleUrl('https://example.com/a b')).toBe(false);
  });

  it('accepts a link of exactly the contract maximum', () => {
    const url = `https://example.com/${'a'.repeat(ARTICLE_URL_MAX_LENGTH - 20)}`;

    expect(url).toHaveLength(ARTICLE_URL_MAX_LENGTH);
    expect(isLikelyArticleUrl(url)).toBe(true);
  });

  it('rejects a link one character over the contract maximum', () => {
    const url = `https://example.com/${'a'.repeat(ARTICLE_URL_MAX_LENGTH - 19)}`;

    expect(isLikelyArticleUrl(url)).toBe(false);
  });

  it('rejects an empty field', () => {
    expect(isLikelyArticleUrl('')).toBe(false);
  });
});

describe('hasUsableSource', () => {
  it('accepts a topic of three characters', () => {
    expect(hasUsableSource(makeFields({ topic: 'DNA' }), null)).toBe(true);
  });

  it('rejects a topic of two characters', () => {
    expect(hasUsableSource(makeFields({ topic: 'ДН' }), null)).toBe(false);
  });

  it('rejects a topic of only whitespace', () => {
    expect(hasUsableSource(makeFields({ topic: '    ' }), null)).toBe(false);
  });

  it('ignores a picked file while the topic source is chosen', () => {
    expect(hasUsableSource(makeFields({ topic: '' }), file)).toBe(false);
  });

  it('accepts a link when the article source is chosen', () => {
    const fields = makeFields({
      source: GENERATION_SOURCE.ARTICLE,
      articleUrl: 'https://example.com/a',
    });

    expect(hasUsableSource(fields, null)).toBe(true);
  });

  it('rejects an empty link when the article source is chosen', () => {
    const fields = makeFields({ source: GENERATION_SOURCE.ARTICLE, articleUrl: '' });

    expect(hasUsableSource(fields, null)).toBe(false);
  });

  it('ignores a typed topic while the article source is chosen', () => {
    const fields = makeFields({ source: GENERATION_SOURCE.ARTICLE, topic: 'Road signs' });

    expect(hasUsableSource(fields, null)).toBe(false);
  });

  it('accepts a picked file when the document source is chosen', () => {
    expect(hasUsableSource(makeFields({ source: GENERATION_SOURCE.DOCUMENT }), file)).toBe(true);
  });

  it('rejects the document source with no file picked', () => {
    expect(hasUsableSource(makeFields({ source: GENERATION_SOURCE.DOCUMENT }), null)).toBe(false);
  });
});
