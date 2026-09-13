import { FlashList } from '@shopify/flash-list';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, View } from 'react-native';

import { useDeckSummary } from '@/entities/deck';
import { NotePreview, useNotesInDeck } from '@/entities/note';
import { deleteDeck } from '@/features/edit-deck';
import { ROUTE_PARAM, routes } from '@/shared/config';
import type { NoteRow } from '@/shared/db';
import { useDayCutoff } from '@/shared/lib';
import { BackHeader, Button, EmptyState, IconButton, Screen, Text } from '@/shared/ui';

const keyExtractor = (note: NoteRow): string => note.id;

export const DeckDetailScreen = () => {
  const { t } = useTranslation('deck');
  const { t: tCommon } = useTranslation('common');
  const router = useRouter();
  const cutoff = useDayCutoff();
  const params = useLocalSearchParams<{ deckId: string }>();
  const deckId = params[ROUTE_PARAM.DECK_ID];

  const { data: summaries } = useDeckSummary(deckId, cutoff);
  const { data: notes } = useNotesInDeck(deckId);
  const summary = summaries[0];
  const dueCards = summary?.dueCards ?? 0;

  const openNote = useCallback(
    (noteId: string) => {
      router.push(routes.noteEditor(deckId, noteId));
    },
    [deckId, router],
  );

  const addNote = useCallback(() => {
    router.push(routes.newNote(deckId));
  }, [deckId, router]);

  const startStudy = useCallback(() => {
    router.push(routes.study(deckId));
  }, [deckId, router]);

  const goBack = useCallback(() => {
    router.back();
  }, [router]);

  const confirmDelete = useCallback(() => {
    Alert.alert(t('deleteConfirm.title'), t('deleteConfirm.description'), [
      { text: tCommon('actions.cancel'), style: 'cancel' },
      {
        text: tCommon('actions.delete'),
        style: 'destructive',
        onPress: () => {
          deleteDeck(deckId);
          router.back();
        },
      },
    ]);
  }, [deckId, router, t, tCommon]);

  const renderItem = useCallback(
    ({ item }: { item: NoteRow }) => (
      <NotePreview
        note={item}
        invalidLabel={t('note.invalid')}
        onPress={() => {
          openNote(item.id);
        }}
      />
    ),
    [openNote, t],
  );

  const isEmpty = notes.length === 0;

  return (
    <Screen
      edges={['top', 'bottom']}
      header={
        <BackHeader
          onBack={goBack}
          accessibilityLabel={tCommon('actions.back')}
          action={
            <IconButton
              name="trash-outline"
              tone="danger"
              size={22}
              accessibilityLabel={t('actions.deleteDeck')}
              onPress={confirmDelete}
            />
          }
        />
      }
      {...(isEmpty ? { footer: <Button label={t('actions.addNote')} onPress={addNote} /> } : {})}
    >
      <View className="gap-1 pb-5 pt-1">
        <Text variant="display" numberOfLines={2}>
          {summary?.title ?? ''}
        </Text>

        {isEmpty ? null : (
          <Text variant="caption" className={dueCards > 0 ? 'text-primary' : 'text-muted'}>
            {`${
              dueCards > 0 ? t('card.dueCount', { count: dueCards }) : t('card.noneDue')
            } · ${t('card.totalCount', { count: summary?.totalCards ?? 0 })}`}
          </Text>
        )}
      </View>

      {isEmpty ? (
        <EmptyState title={t('empty.notesTitle')} description={t('empty.notesDescription')} />
      ) : (
        <>
          <View className="flex-row gap-3 pb-4">
            <View className="flex-1">
              <Button label={t('actions.addNote')} variant="secondary" onPress={addNote} />
            </View>
            <View className="flex-1">
              <Button label={t('actions.study')} disabled={dueCards === 0} onPress={startStudy} />
            </View>
          </View>

          <FlashList
            data={notes}
            renderItem={renderItem}
            keyExtractor={keyExtractor}
            contentInsetAdjustmentBehavior="never"
            automaticallyAdjustContentInsets={false}
            maintainVisibleContentPosition={{ disabled: true }}
            contentContainerClassName="pb-section"
            ItemSeparatorComponent={() => <View className="h-3" />}
            showsVerticalScrollIndicator={false}
          />
        </>
      )}
    </Screen>
  );
};
