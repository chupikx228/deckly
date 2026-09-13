import type { GeneratedNote } from '@deckly/api-contract';
import { memo, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import {
  FORWARD_ORDINAL,
  deriveCardOrdinals,
  getCardFaces,
  safeParseTypedNote,
} from '@/entities/note';
import { Button, Card, Text } from '@/shared/ui';

export interface GeneratedNoteCardProps {
  note: GeneratedNote;
  isRegenerating: boolean;
  onRegenerate: (clientId: string) => void;
  onDiscard: (clientId: string) => void;
}

export const GeneratedNoteCard = memo(
  ({ note, isRegenerating, onRegenerate, onDiscard }: GeneratedNoteCardProps) => {
    const { t } = useTranslation('generation');
    const { t: tDeck } = useTranslation('deck');

    const typed = safeParseTypedNote(note.noteType, note.fields);

    const handleRegenerate = useCallback(() => {
      onRegenerate(note.clientId);
    }, [note.clientId, onRegenerate]);

    const handleDiscard = useCallback(() => {
      onDiscard(note.clientId);
    }, [note.clientId, onDiscard]);

    if (typed === null) {
      return (
        <Card>
          <Text variant="body" className="text-danger">
            {tDeck('note.invalid')}
          </Text>
        </Card>
      );
    }

    const [firstOrdinal] = deriveCardOrdinals(typed);
    const faces = getCardFaces(typed, firstOrdinal ?? FORWARD_ORDINAL);

    return (
      <Card>
        <View className="gap-3">
          <View className="gap-1">
            <Text variant="label">{faces.question}</Text>
            <Text variant="body" muted>
              {faces.answer}
            </Text>
          </View>

          {note.sources.length > 0 ? (
            <Text variant="caption" numberOfLines={1}>
              {`${t('preview.sources')}: ${note.sources[0]?.title ?? ''}`}
            </Text>
          ) : null}

          <View className="flex-row gap-2">
            <View className="flex-1">
              <Button
                label={t('preview.regenerate')}
                variant="secondary"
                size="small"
                isLoading={isRegenerating}
                onPress={handleRegenerate}
              />
            </View>
            <View className="flex-1">
              <Button
                label={t('preview.discard')}
                variant="ghost"
                size="small"
                onPress={handleDiscard}
              />
            </View>
          </View>
        </View>
      </Card>
    );
  },
);

GeneratedNoteCard.displayName = 'GeneratedNoteCard';
