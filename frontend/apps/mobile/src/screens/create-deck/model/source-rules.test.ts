import { describe, expect, it } from 'vitest';

import { ARTICLE_URL_MAX_LENGTH, RECORDING_URL_MAX_LENGTH } from '@deckly/api-contract';

import type { DocumentUpload } from '@/shared/api';
import { DIFFICULTY, LOCALE, NOTE_TYPE } from '@/shared/config';

import {
  hasUsableSource,
  isLikelyArticleUrl,
  isRecordingUrl,
  resolveSubmitRoute,
} from './source-rules';
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

  it('accepts a picked file when the recording source is chosen', () => {
    expect(hasUsableSource(makeFields({ source: GENERATION_SOURCE.RECORDING }), file)).toBe(true);
  });

  it('rejects the recording source with no file picked', () => {
    expect(hasUsableSource(makeFields({ source: GENERATION_SOURCE.RECORDING }), null)).toBe(false);
  });
});

const recording: DocumentUpload = {
  uri: 'file:///tmp/lecture.m4a',
  name: 'lecture.m4a',
  mimeType: 'audio/mp4',
  sizeBytes: 7_400_000,
};

describe('isRecordingUrl', () => {
  it('accepts a watch link', () => {
    expect(isRecordingUrl('https://www.youtube.com/watch?v=jNQXAC9IVRw')).toBe(true);
  });

  it('accepts a link with no www', () => {
    expect(isRecordingUrl('https://youtube.com/watch?v=jNQXAC9IVRw')).toBe(true);
  });

  it('accepts the mobile and music hosts', () => {
    expect(isRecordingUrl('https://m.youtube.com/watch?v=jNQXAC9IVRw')).toBe(true);
    expect(isRecordingUrl('https://music.youtube.com/watch?v=jNQXAC9IVRw')).toBe(true);
  });

  it('accepts a short link', () => {
    expect(isRecordingUrl('https://youtu.be/jNQXAC9IVRw')).toBe(true);
  });

  it('accepts shorts, live and embed links', () => {
    expect(isRecordingUrl('https://www.youtube.com/shorts/abc')).toBe(true);
    expect(isRecordingUrl('https://www.youtube.com/live/abc')).toBe(true);
    expect(isRecordingUrl('https://www.youtube.com/embed/abc')).toBe(true);
  });

  it('claims a playlist link so the server can name it, not the article endpoint', () => {
    expect(isRecordingUrl('https://www.youtube.com/playlist?list=PL1')).toBe(true);
  });

  it('claims a channel link for the same reason', () => {
    expect(isRecordingUrl('https://www.youtube.com/@someone')).toBe(true);
  });

  it('ignores case in the host', () => {
    expect(isRecordingUrl('HTTPS://WWW.YOUTUBE.COM/watch?v=abc')).toBe(true);
  });

  it('ignores surrounding whitespace', () => {
    expect(isRecordingUrl('  https://youtu.be/abc  ')).toBe(true);
  });

  it('rejects a host that merely starts with a youtube name', () => {
    expect(isRecordingUrl('https://www.youtube.com.evil.test/watch?v=abc')).toBe(false);
    expect(isRecordingUrl('https://youtu.be.evil.test/abc')).toBe(false);
  });

  it('rejects a host that merely ends with a youtube name', () => {
    expect(isRecordingUrl('https://notyoutube.com/watch?v=abc')).toBe(false);
  });

  it('rejects an ordinary article link', () => {
    expect(isRecordingUrl('https://en.wikipedia.org/wiki/Spaced_repetition')).toBe(false);
  });
});

describe('the url field is checked against one length for both endpoints', () => {
  it('keeps the article and recording maximums equal', () => {
    expect(ARTICLE_URL_MAX_LENGTH).toBe(RECORDING_URL_MAX_LENGTH);
  });
});

describe('resolveSubmitRoute', () => {
  it('trims the topic it sends', () => {
    expect(resolveSubmitRoute(makeFields({ topic: '  Road signs  ' }), null)).toEqual({
      kind: 'topic',
      topic: 'Road signs',
    });
  });

  it('refuses a source that is not usable yet', () => {
    expect(resolveSubmitRoute(makeFields({ topic: 'DN' }), null)).toBeNull();
  });

  it('sends a picked document to the document route', () => {
    const fields = makeFields({ source: GENERATION_SOURCE.DOCUMENT });

    expect(resolveSubmitRoute(fields, file)).toEqual({ kind: 'document', file });
  });

  it('sends a picked recording to the recording route', () => {
    const fields = makeFields({ source: GENERATION_SOURCE.RECORDING });

    expect(resolveSubmitRoute(fields, recording)).toEqual({ kind: 'recording', file: recording });
  });

  it('refuses a file source with nothing picked', () => {
    expect(
      resolveSubmitRoute(makeFields({ source: GENERATION_SOURCE.RECORDING }), null),
    ).toBeNull();
  });

  it('sends an article link to the article route', () => {
    const fields = makeFields({
      source: GENERATION_SOURCE.ARTICLE,
      articleUrl: 'https://en.wikipedia.org/wiki/Spaced_repetition',
    });

    expect(resolveSubmitRoute(fields, null)).toEqual({
      kind: 'article',
      url: 'https://en.wikipedia.org/wiki/Spaced_repetition',
    });
  });

  it('sends a youtube link to the recording route from the same field', () => {
    const fields = makeFields({
      source: GENERATION_SOURCE.ARTICLE,
      articleUrl: '  https://youtu.be/jNQXAC9IVRw  ',
    });

    expect(resolveSubmitRoute(fields, null)).toEqual({
      kind: 'recordingUrl',
      url: 'https://youtu.be/jNQXAC9IVRw',
    });
  });

  it('never routes a file left over from another source', () => {
    const fields = makeFields({
      source: GENERATION_SOURCE.ARTICLE,
      articleUrl: 'https://example.com/a',
    });

    expect(resolveSubmitRoute(fields, file)).toEqual({
      kind: 'article',
      url: 'https://example.com/a',
    });
  });
});
