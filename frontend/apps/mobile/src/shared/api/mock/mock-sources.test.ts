import {
  DOCUMENT_CONTENT_TYPE,
  GENERATABLE_DOCUMENT_TYPES,
  GENERATABLE_RECORDING_TYPES,
} from '@deckly/api-contract';
import { describe, expect, it } from 'vitest';

import type { DocumentUpload } from '../transport';
import { isYouTubeVideoUrl, toContentType, toSourceFilename } from './mock-sources';

const upload = (name: string, mimeType: string): DocumentUpload => ({
  uri: `file:///tmp/${name}`,
  name,
  mimeType,
  sizeBytes: 1_024,
});

describe('isYouTubeVideoUrl', () => {
  it('accepts a single video', () => {
    expect(isYouTubeVideoUrl('https://www.youtube.com/watch?v=jNQXAC9IVRw')).toBe(true);
  });

  it('accepts a short link', () => {
    expect(isYouTubeVideoUrl('https://youtu.be/jNQXAC9IVRw')).toBe(true);
  });

  it('refuses a playlist, as the endpoint does', () => {
    expect(isYouTubeVideoUrl('https://www.youtube.com/playlist?list=PL1')).toBe(false);
  });

  it('refuses a video carrying a playlist, as the endpoint does', () => {
    expect(isYouTubeVideoUrl('https://www.youtube.com/watch?v=abc&list=PL1')).toBe(false);
  });

  it('refuses a channel', () => {
    expect(isYouTubeVideoUrl('https://www.youtube.com/@someone')).toBe(false);
  });

  it('refuses another site', () => {
    expect(isYouTubeVideoUrl('https://vimeo.com/123')).toBe(false);
  });
});

describe('toContentType', () => {
  it('takes the declared type when it is allowed', () => {
    expect(toContentType(upload('talk.m4a', 'audio/mp4'), GENERATABLE_RECORDING_TYPES)).toBe(
      DOCUMENT_CONTENT_TYPE.M4A,
    );
  });

  it('falls back to the extension when the picker declared nothing', () => {
    expect(toContentType(upload('talk.mp3', ''), GENERATABLE_RECORDING_TYPES)).toBe(
      DOCUMENT_CONTENT_TYPE.MP3,
    );
  });

  it('refuses a recording offered as a document', () => {
    expect(
      toContentType(upload('talk.m4a', 'audio/mp4'), GENERATABLE_DOCUMENT_TYPES),
    ).toBeUndefined();
  });

  it('refuses a document offered as a recording', () => {
    expect(
      toContentType(upload('slides.pdf', 'application/pdf'), GENERATABLE_RECORDING_TYPES),
    ).toBeUndefined();
  });

  it('refuses a known extension that the chosen source does not allow', () => {
    expect(toContentType(upload('slides.pdf', ''), GENERATABLE_RECORDING_TYPES)).toBeUndefined();
  });

  it('refuses a file with no extension and no declared type', () => {
    expect(toContentType(upload('recording', ''), GENERATABLE_RECORDING_TYPES)).toBeUndefined();
  });

  it('ignores the case of the extension', () => {
    expect(toContentType(upload('TALK.MOV', ''), GENERATABLE_RECORDING_TYPES)).toBe(
      DOCUMENT_CONTENT_TYPE.MOV,
    );
  });
});

describe('toSourceFilename', () => {
  it('names a stored source after its host', () => {
    expect(toSourceFilename('https://en.wikipedia.org/wiki/Spaced_repetition')).toBe(
      'en.wikipedia.org',
    );
  });

  it('keeps a bare host', () => {
    expect(toSourceFilename('https://youtu.be')).toBe('youtu.be');
  });
});
