import { View } from 'react-native';

import type { NoteRow } from '@/shared/db';
import { Card, Text } from '@/shared/ui';

import { deriveCardOrdinals, getCardFaces, FORWARD_ORDINAL } from '../model/card-templates';
import { safeParseTypedNote } from '../model/note-fields';

export interface NotePreviewProps {
  note: NoteRow;
  invalidLabel: string;
  onPress?: () => void;
}

export const NotePreview = ({ note, invalidLabel, onPress }: NotePreviewProps) => {
  const typed = safeParseTypedNote(note.noteType, note.fields);

  if (typed === null) {
    return (
      <Card onPress={onPress}>
        <Text variant="body" className="text-danger">
          {invalidLabel}
        </Text>
      </Card>
    );
  }

  const [firstOrdinal] = deriveCardOrdinals(typed);
  const faces = getCardFaces(typed, firstOrdinal ?? FORWARD_ORDINAL);

  return (
    <Card onPress={onPress}>
      <View className="gap-1">
        <Text variant="label" numberOfLines={2}>
          {faces.question}
        </Text>
        <Text variant="body" muted numberOfLines={2}>
          {faces.answer}
        </Text>
      </View>
    </Card>
  );
};
