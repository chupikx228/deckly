import { FlashList } from '@shopify/flash-list';
import { useRouter } from 'expo-router';
import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { useDeckSummaries, type DeckSummary } from '@/entities/deck';
import { routes } from '@/shared/config';
import { useDayCutoff } from '@/shared/lib';
import { Button, EmptyState, Screen, Text } from '@/shared/ui';

import { DeckComposer } from './deck-composer';
import { DeckListItem } from './deck-list-item';

const keyExtractor = (deck: DeckSummary): string => deck.id;

export const DecksScreen = () => {
  const { t } = useTranslation('deck');
  const router = useRouter();
  const [isComposing, setIsComposing] = useState(false);
  const cutoff = useDayCutoff();
  const { data } = useDeckSummaries(cutoff);

  const openDeck = useCallback(
    (deckId: string) => {
      router.push(routes.deckDetail(deckId));
    },
    [router],
  );

  const handleCreated = useCallback(
    (deckId: string) => {
      setIsComposing(false);
      openDeck(deckId);
    },
    [openDeck],
  );

  const renderItem = useCallback(
    ({ item }: { item: DeckSummary }) => <DeckListItem deck={item} onPress={openDeck} />,
    [openDeck],
  );

  const startComposing = useCallback(() => {
    setIsComposing(true);
  }, []);

  const stopComposing = useCallback(() => {
    setIsComposing(false);
  }, []);

  return (
    <Screen>
      <View className="gap-4 pb-4 pt-2">
        <Text variant="display">{t('list.all')}</Text>

        {isComposing ? (
          <DeckComposer onCreated={handleCreated} onCancel={stopComposing} />
        ) : data.length > 0 ? (
          <Button label={t('actions.createDeck')} variant="secondary" onPress={startComposing} />
        ) : null}
      </View>

      {data.length === 0 ? (
        isComposing ? null : (
          <EmptyState
            title={t('empty.title')}
            description={t('empty.description')}
            actionLabel={t('actions.createDeck')}
            onAction={startComposing}
          />
        )
      ) : (
        <FlashList
          data={data}
          renderItem={renderItem}
          keyExtractor={keyExtractor}
          contentInsetAdjustmentBehavior="never"
          automaticallyAdjustContentInsets={false}
          contentContainerClassName="pb-section"
          ItemSeparatorComponent={() => <View className="h-3" />}
          showsVerticalScrollIndicator={false}
        />
      )}
    </Screen>
  );
};
