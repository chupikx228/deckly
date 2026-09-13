import { memo } from 'react';
import { useTranslation } from 'react-i18next';

import { DeckCard, type DeckSummary } from '@/entities/deck';

export interface DeckListItemProps {
  deck: DeckSummary;
  onPress: (deckId: string) => void;
}

export const DeckListItem = memo(({ deck, onPress }: DeckListItemProps) => {
  const { t } = useTranslation('deck');

  return (
    <DeckCard
      deck={deck}
      dueLabel={
        deck.dueCards > 0 ? t('card.dueCount', { count: deck.dueCards }) : t('card.noneDue')
      }
      totalLabel={t('card.totalCount', { count: deck.totalCards })}
      onPress={onPress}
    />
  );
});

DeckListItem.displayName = 'DeckListItem';
