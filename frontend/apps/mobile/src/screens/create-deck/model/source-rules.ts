import { ARTICLE_URL_MAX_LENGTH } from '@deckly/api-contract';

import type { DocumentUpload } from '@/shared/api';

import type { WizardFields } from './use-wizard-fields';
import { GENERATION_SOURCE } from './wizard-options';

const MIN_TOPIC_LENGTH = 3;

const ARTICLE_URL = /^https?:\/\/[^\s/?#@]+\.[^\s/?#@]+(?:[/?#]\S*)?$/i;

export const isLikelyArticleUrl = (value: string): boolean => {
  const trimmed = value.trim();

  return trimmed.length <= ARTICLE_URL_MAX_LENGTH && ARTICLE_URL.test(trimmed);
};

export const hasUsableSource = (fields: WizardFields, file: DocumentUpload | null): boolean => {
  if (fields.source === GENERATION_SOURCE.TOPIC) {
    return fields.topic.trim().length >= MIN_TOPIC_LENGTH;
  }

  if (fields.source === GENERATION_SOURCE.ARTICLE) {
    return isLikelyArticleUrl(fields.articleUrl);
  }

  return file !== null;
};
