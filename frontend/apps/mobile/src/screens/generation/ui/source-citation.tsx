import { SOURCE_KIND, type Source } from '@deckly/api-contract';
import { useTranslation } from 'react-i18next';

import { Text } from '@/shared/ui';

import { DOCUMENT_UNIT_CITATION_KEY, formatTimestamp } from '../model/source-citation';

export interface SourceCitationProps {
  source: Source;
}

export const SourceCitation = ({ source }: SourceCitationProps) => {
  const { t } = useTranslation('generation');

  if (source.kind === SOURCE_KIND.DOCUMENT) {
    return (
      <Text variant="caption" numberOfLines={1}>
        {t(DOCUMENT_UNIT_CITATION_KEY[source.unit], {
          title: source.title,
          number: source.number,
        })}
      </Text>
    );
  }

  if (source.kind === SOURCE_KIND.TRANSCRIPT) {
    return (
      <Text variant="caption" numberOfLines={1}>
        {t('source.transcript', {
          title: source.title,
          from: formatTimestamp(source.startMs),
          to: formatTimestamp(source.endMs),
        })}
      </Text>
    );
  }

  return (
    <Text variant="caption" numberOfLines={1}>
      {t('source.web', { title: source.title })}
    </Text>
  );
};
