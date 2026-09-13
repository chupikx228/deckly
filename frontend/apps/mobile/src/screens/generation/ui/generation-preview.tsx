import type { GeneratedDeck, GeneratedNote } from '@deckly/api-contract';
import { FlashList } from '@shopify/flash-list';
import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { importGeneratedDeck, useRegenerateNote } from '@/features/generate-deck';
import type { Locale } from '@/shared/config';
import { BackHeader, Button, EmptyState, Screen, Text } from '@/shared/ui';

import { GeneratedNoteCard } from './generated-note-card';

export interface GenerationPreviewProps {
  jobId: string;
  deck: GeneratedDeck;
  initialNotes: readonly GeneratedNote[];
  topic: string;
  language: Locale;
  onSaved: (deckId: string) => void;
  onDiscardAll: () => void;
}

const keyExtractor = (note: GeneratedNote): string => note.clientId;

export const GenerationPreview = ({
  jobId,
  deck,
  initialNotes,
  topic,
  language,
  onSaved,
  onDiscardAll,
}: GenerationPreviewProps) => {
  const { t } = useTranslation('generation');
  const { t: tCommon } = useTranslation('common');
  const [notes, setNotes] = useState<readonly GeneratedNote[]>(initialNotes);
  const [regeneratingId, setRegeneratingId] = useState<string | null>(null);

  const regenerate = useRegenerateNote();

  const handleDiscard = useCallback((clientId: string) => {
    setNotes((current) => current.filter((note) => note.clientId !== clientId));
  }, []);

  const handleRegenerate = useCallback(
    (clientId: string) => {
      const target = notes.find((note) => note.clientId === clientId);

      if (target === undefined) {
        return;
      }

      setRegeneratingId(clientId);

      regenerate.mutate(
        {
          topic,
          language,
          noteType: target.noteType,
          rejectedNote: { fields: target.fields },
        },
        {
          onSuccess: (replacement) => {
            setNotes((current) =>
              current.map((note) =>
                note.clientId === clientId ? { ...replacement, clientId } : note,
              ),
            );
          },
          onSettled: () => {
            setRegeneratingId(null);
          },
        },
      );
    },
    [language, notes, regenerate, topic],
  );

  const handleSave = useCallback(() => {
    const result = importGeneratedDeck({ deck, notes, jobId });

    onSaved(result.deckId);
  }, [deck, jobId, notes, onSaved]);

  const renderItem = useCallback(
    ({ item }: { item: GeneratedNote }) => (
      <GeneratedNoteCard
        note={item}
        isRegenerating={regeneratingId === item.clientId}
        onRegenerate={handleRegenerate}
        onDiscard={handleDiscard}
      />
    ),
    [handleDiscard, handleRegenerate, regeneratingId],
  );

  if (notes.length === 0) {
    return (
      <Screen>
        <EmptyState
          title={t('preview.title')}
          description={t('preview.empty')}
          actionLabel={t('preview.discard')}
          onAction={onDiscardAll}
        />
      </Screen>
    );
  }

  return (
    <Screen
      header={<BackHeader onBack={onDiscardAll} accessibilityLabel={tCommon('actions.close')} />}
    >
      <View className="gap-1 py-3">
        <Text variant="title">{t('preview.title')}</Text>
        <Text variant="caption">{t('preview.noteCount', { count: notes.length })}</Text>
      </View>

      <FlashList
        data={notes}
        renderItem={renderItem}
        keyExtractor={keyExtractor}
        contentInsetAdjustmentBehavior="never"
        automaticallyAdjustContentInsets={false}
        ItemSeparatorComponent={() => <View className="h-3" />}
        showsVerticalScrollIndicator={false}
      />

      <View className="gap-2 py-4">
        <Button label={t('preview.save')} onPress={handleSave} />
        <Button label={t('preview.discard')} variant="ghost" onPress={onDiscardAll} />
      </View>
    </Screen>
  );
};
