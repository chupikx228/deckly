import { ARTICLE_URL_MAX_LENGTH } from '@deckly/api-contract';

import type { DocumentUpload } from '@/shared/api';

import type { WizardFields } from './use-wizard-fields';
import { GENERATION_SOURCE } from './wizard-options';

const MIN_TOPIC_LENGTH = 3;

const ARTICLE_URL = /^https?:\/\/[^\s/?#@]+\.[^\s/?#@]+(?:[/?#]\S*)?$/i;

const YOUTUBE_HOST = /^https?:\/\/(?:(?:www\.|m\.|music\.)?youtube\.com|youtu\.be)(?:[/?#]|$)/i;

export const isLikelyArticleUrl = (value: string): boolean => {
  const trimmed = value.trim();

  return trimmed.length <= ARTICLE_URL_MAX_LENGTH && ARTICLE_URL.test(trimmed);
};

export const isRecordingUrl = (value: string): boolean => YOUTUBE_HOST.test(value.trim());

export const hasUsableSource = (fields: WizardFields, file: DocumentUpload | null): boolean => {
  if (fields.source === GENERATION_SOURCE.TOPIC) {
    return fields.topic.trim().length >= MIN_TOPIC_LENGTH;
  }

  if (fields.source === GENERATION_SOURCE.ARTICLE) {
    return isLikelyArticleUrl(fields.articleUrl);
  }

  return file !== null;
};

export type SubmitRoute =
  | { readonly kind: 'topic'; readonly topic: string }
  | { readonly kind: 'document'; readonly file: DocumentUpload }
  | { readonly kind: 'recording'; readonly file: DocumentUpload }
  | { readonly kind: 'article'; readonly url: string }
  | { readonly kind: 'recordingUrl'; readonly url: string };

export const resolveSubmitRoute = (
  fields: WizardFields,
  file: DocumentUpload | null,
): SubmitRoute | null => {
  if (!hasUsableSource(fields, file)) {
    return null;
  }

  if (fields.source === GENERATION_SOURCE.TOPIC) {
    return { kind: 'topic', topic: fields.topic.trim() };
  }

  if (fields.source === GENERATION_SOURCE.ARTICLE) {
    const url = fields.articleUrl.trim();

    return isRecordingUrl(url) ? { kind: 'recordingUrl', url } : { kind: 'article', url };
  }

  if (file === null) {
    return null;
  }

  return fields.source === GENERATION_SOURCE.RECORDING
    ? { kind: 'recording', file }
    : { kind: 'document', file };
};
