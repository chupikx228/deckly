import { DOCUMENT_CONTENT_TYPE, type DocumentContentType } from '@deckly/api-contract';

import type { DocumentUpload, UploadProgressHandler } from '../transport';

export const MOCK_ARTICLE_MS = 900;
export const MOCK_ARTICLE_SIZE = 48_000;

export const MOCK_RECORDING_MS = 1_600;
export const MOCK_RECORDING_SIZE = 7_400_000;
export const MOCK_RECORDING_SECONDS = 540;

export const MOCK_LONG_RECORDING_MARKER = 'long';
export const MOCK_UNAVAILABLE_VIDEO_MARKER = 'unavailable';

const MOCK_UPLOAD_STEPS = 5;
const MOCK_UPLOAD_STEP_MS = 160;

const ARTICLE_URL = /^https?:\/\/[^\s/?#@]+\.[^\s/?#@]+(?:[/?#]\S*)?$/i;

const YOUTUBE_VIDEO_URL =
  /^https?:\/\/(?:(?:www\.|m\.|music\.)?youtube\.com\/(?:watch\?|shorts\/|live\/|embed\/)|youtu\.be\/)\S+$/i;

const EXTENSION_CONTENT_TYPE: Readonly<Record<string, DocumentContentType>> = {
  pdf: DOCUMENT_CONTENT_TYPE.PDF,
  pptx: DOCUMENT_CONTENT_TYPE.PPTX,
  mp3: DOCUMENT_CONTENT_TYPE.MP3,
  m4a: DOCUMENT_CONTENT_TYPE.M4A,
  wav: DOCUMENT_CONTENT_TYPE.WAV,
  mp4: DOCUMENT_CONTENT_TYPE.MP4,
  mov: DOCUMENT_CONTENT_TYPE.MOV,
};

export const isArticleUrl = (url: string): boolean => ARTICLE_URL.test(url);

export const isYouTubeVideoUrl = (url: string): boolean =>
  YOUTUBE_VIDEO_URL.test(url) && !/[?&]list=/i.test(url);

export const toContentType = (
  file: DocumentUpload,
  allowed: readonly DocumentContentType[],
): DocumentContentType | undefined => {
  const declared = allowed.find((type) => type === file.mimeType);

  if (declared !== undefined) {
    return declared;
  }

  const byExtension = EXTENSION_CONTENT_TYPE[file.name.split('.').pop()?.toLowerCase() ?? ''];

  return byExtension === undefined || !allowed.includes(byExtension) ? undefined : byExtension;
};

export const toSourceFilename = (url: string): string => {
  const withoutScheme = url.replace(/^https?:\/\//i, '');

  return withoutScheme.split('/')[0] ?? withoutScheme;
};

export const wait = (ms: number): Promise<void> =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

export const simulateUpload = (onProgress: UploadProgressHandler): Promise<void> =>
  new Promise((resolve) => {
    let step = 0;

    const tick = (): void => {
      step += 1;
      onProgress(step / MOCK_UPLOAD_STEPS);

      if (step >= MOCK_UPLOAD_STEPS) {
        resolve();
        return;
      }

      setTimeout(tick, MOCK_UPLOAD_STEP_MS);
    };

    setTimeout(tick, MOCK_UPLOAD_STEP_MS);
  });
