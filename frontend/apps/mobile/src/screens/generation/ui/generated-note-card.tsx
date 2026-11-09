import type { GeneratedNote, RejectionReason } from '@deckly/api-contract';
import { memo, useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import {
  FORWARD_ORDINAL,
  deriveCardOrdinals,
  getCardFaces,
  safeParseTypedNote,
} from '@/entities/note';
import { Button, Card, Text } from '@/shared/ui';

import { ReasonPicker } from './reason-picker';
import { SourceCitation } from './source-citation';

export interface GeneratedNoteCardProps {
  note: GeneratedNote;
  isRegenerating: boolean;
  onRegenerate: (clientId: string, reason: RejectionReason) => void;
  onDiscard: (clientId: string) => void;
}

export const GeneratedNoteCard = memo(
  ({ note, isRegenerating, onRegenerate, onDiscard }: GeneratedNoteCardProps) => {
    const { t } = useTranslation('generation');
    const { t: tDeck } = useTranslation('deck');

    const [isPickingReason, setIsPickingReason] = useState(false);

    const typed = safeParseTypedNote(note.noteType, note.fields);

    const handleAskReason = useCallback(() => {
      setIsPickingReason(true);
    }, []);

    const handleCancelReason = useCallback(() => {
      setIsPickingReason(false);
    }, []);

    const handlePickReason = useCallback(
      (reason: RejectionReason) => {
        setIsPickingReason(false);
        onRegenerate(note.clientId, reason);
      },
      [note.clientId, onRegenerate],
    );

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
    const [firstSource] = note.sources;
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

          <SourceCitation source={firstSource} />

          {isPickingReason ? (
            <ReasonPicker onPick={handlePickReason} onCancel={handleCancelReason} />
          ) : (
            <View className="flex-row gap-2">
              <View className="flex-1">
                <Button
                  label={t('preview.regenerate')}
                  variant="secondary"
                  size="small"
                  isLoading={isRegenerating}
                  onPress={handleAskReason}
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
          )}
        </View>
      </Card>
    );
  },
);

GeneratedNoteCard.displayName = 'GeneratedNoteCard';
