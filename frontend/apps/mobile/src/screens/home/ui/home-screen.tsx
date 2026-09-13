import { useRouter } from 'expo-router';
import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { useDueCardCount } from '@/entities/card';
import { DeckCard, useRecentDeckSummaries } from '@/entities/deck';
import { routes } from '@/shared/config';
import { useDayCutoff } from '@/shared/lib';
import { Button, Card, Screen, Text } from '@/shared/ui';

const RECENT_DECK_LIMIT = 5;

export const HomeScreen = () => {
  const { t } = useTranslation('deck');
  const router = useRouter();
  const cutoff = useDayCutoff();

  const { data: recentDecks } = useRecentDeckSummaries(cutoff, RECENT_DECK_LIMIT);
  const { data: dueRows } = useDueCardCount(cutoff);
  const dueTotal = dueRows[0]?.total ?? 0;

  const openDeck = useCallback(
    (deckId: string) => {
      router.push(routes.deckDetail(deckId));
    },
    [router],
  );

  const openDecks = useCallback(() => {
    router.push(routes.decks());
  }, [router]);

  return (
    <Screen scrollable>
      <Text variant="display">{t('list.recent')}</Text>

      <Card>
        <Text variant="heading">
          {dueTotal > 0 ? t('card.dueCount', { count: dueTotal }) : t('card.noneDue')}
        </Text>
        {dueTotal === 0 ? (
          <Text variant="body" className="mt-1 text-foreground/60">
            {t('card.noneDueHint')}
          </Text>
        ) : null}
      </Card>

      {recentDecks.length === 0 ? (
        <Card>
          <Text variant="heading">{t('empty.title')}</Text>
          <Text variant="body" muted className="mt-1">
            {t('empty.description')}
          </Text>
          <View className="mt-4">
            <Button label={t('actions.createDeck')} onPress={openDecks} />
          </View>
        </Card>
      ) : (
        <View className="gap-3">
          {recentDecks.map((deck) => (
            <DeckCard
              key={deck.id}
              deck={deck}
              dueLabel={
                deck.dueCards > 0 ? t('card.dueCount', { count: deck.dueCards }) : t('card.noneDue')
              }
              totalLabel={t('card.totalCount', { count: deck.totalCards })}
              onPress={openDeck}
            />
          ))}
        </View>
      )}
    </Screen>
  );
};
