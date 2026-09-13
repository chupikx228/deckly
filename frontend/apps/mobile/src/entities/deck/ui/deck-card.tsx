import { useCallback } from 'react';
import { View } from 'react-native';

import { Card, Text } from '@/shared/ui';

import type { DeckSummary } from '../model/deck.types';

export interface DeckCardProps {
  deck: DeckSummary;
  dueLabel: string;
  totalLabel: string;
  onPress: (deckId: string) => void;
}

export const DeckCard = ({ deck, dueLabel, totalLabel, onPress }: DeckCardProps) => {
  const handlePress = useCallback(() => {
    onPress(deck.id);
  }, [deck.id, onPress]);

  const hasDue = deck.dueCards > 0;

  return (
    <Card onPress={handlePress}>
      <View className="flex-row items-center gap-3">
        <View
          className={['h-10 w-1 rounded-full', hasDue ? 'bg-primary' : 'bg-border'].join(' ')}
        />

        <View className="flex-1 gap-0.5">
          <Text variant="heading" numberOfLines={1}>
            {deck.title}
          </Text>

          <Text variant="caption" className={hasDue ? 'text-primary' : 'text-muted'}>
            {`${dueLabel} · ${totalLabel}`}
          </Text>
        </View>
      </View>
    </Card>
  );
};
