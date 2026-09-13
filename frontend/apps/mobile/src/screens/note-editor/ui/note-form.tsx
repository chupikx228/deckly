import { useCallback, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import {
  CLOZE_SYNTAX_EXAMPLE,
  CLOZE_SYNTAX_HINT,
  FORWARD_ORDINAL,
  deriveCardOrdinals,
  getCardFaces,
  safeParseTypedNote,
  validateClozeText,
} from '@/entities/note';
import { NOTE_TYPE } from '@/shared/config';
import { BackHeader, Button, Card, OptionList, Screen, Text, TextField } from '@/shared/ui';

import {
  EDITABLE_NOTE_TYPES,
  NOTE_TYPE_HINT_KEY,
  NOTE_TYPE_LABEL_KEY,
  type EditableNoteType,
  type NoteFormValues,
} from '../model/note-form-values';

export interface NoteFormProps {
  initialValues: NoteFormValues;
  isNew: boolean;
  onSave: (noteType: EditableNoteType, fields: Record<string, unknown>) => void;
  onDelete: () => void;
  onBack: () => void;
}

export const NoteForm = ({ initialValues, isNew, onSave, onDelete, onBack }: NoteFormProps) => {
  const { t } = useTranslation('deck');
  const { t: tCommon } = useTranslation('common');
  const { t: tGeneration } = useTranslation('generation');

  const [noteType, setNoteType] = useState<EditableNoteType>(initialValues.noteType);
  const [front, setFront] = useState(initialValues.front);
  const [back, setBack] = useState(initialValues.back);
  const [text, setText] = useState(initialValues.text);
  const [extra, setExtra] = useState(initialValues.extra);

  const isCloze = noteType === NOTE_TYPE.CLOZE;

  const fields = useMemo(
    () => (isCloze ? { text, extra } : { front, back }),
    [back, extra, front, isCloze, text],
  );

  const typedNote = useMemo(() => safeParseTypedNote(noteType, fields), [fields, noteType]);
  const clozeError = isCloze && text.trim().length > 0 ? validateClozeText(text) : null;
  const cardCount = typedNote === null ? 0 : deriveCardOrdinals(typedNote).length;
  const canSave = typedNote !== null && clozeError === null && cardCount > 0;

  const previewFaces = useMemo(() => {
    if (typedNote === null || clozeError !== null) {
      return null;
    }

    const [firstOrdinal] = deriveCardOrdinals(typedNote);

    return getCardFaces(typedNote, firstOrdinal ?? FORWARD_ORDINAL);
  }, [clozeError, typedNote]);

  const typeOptions = useMemo(
    () =>
      EDITABLE_NOTE_TYPES.map((type) => ({
        value: type,
        label: tGeneration(NOTE_TYPE_LABEL_KEY[type]),
        description: t(NOTE_TYPE_HINT_KEY[type]),
      })),
    [t, tGeneration],
  );

  const handleSave = useCallback(() => {
    if (canSave) {
      onSave(noteType, fields);
    }
  }, [canSave, fields, noteType, onSave]);

  return (
    <Screen
      scrollable
      edges={['top', 'bottom']}
      header={<BackHeader onBack={onBack} accessibilityLabel={tCommon('actions.back')} />}
      footer={
        <View className="gap-2">
          {canSave ? (
            <Text variant="caption" className="text-center">
              {t('note.cardCount', { count: cardCount })}
            </Text>
          ) : null}

          <Button label={tCommon('actions.save')} disabled={!canSave} onPress={handleSave} />

          {isNew ? null : (
            <Button label={t('actions.deleteNote')} variant="ghost" onPress={onDelete} />
          )}
        </View>
      }
    >
      <Text variant="display">{isNew ? t('note.newTitle') : t('note.editTitle')}</Text>

      <View className="gap-2">
        <Text variant="overline">{t('note.typeLabel')}</Text>
        <OptionList options={typeOptions} value={noteType} onChange={setNoteType} />
      </View>

      {isCloze ? (
        <View className="gap-4">
          <TextField
            label={t('note.textLabel')}
            placeholder={t('note.textPlaceholder', { example: CLOZE_SYNTAX_EXAMPLE })}
            value={text}
            multiline
            autoFocus={isNew}
            onChangeText={setText}
            {...(clozeError === null ? {} : { error: t('note.clozeInvalid') })}
          />
          <Text variant="caption">{t('note.clozeHelp', { syntax: CLOZE_SYNTAX_HINT })}</Text>
          <TextField
            label={t('note.extraLabel')}
            placeholder={t('note.extraPlaceholder')}
            value={extra}
            onChangeText={setExtra}
          />
        </View>
      ) : (
        <View className="gap-4">
          <TextField
            label={t('note.frontLabel')}
            placeholder={t('note.frontPlaceholder')}
            value={front}
            autoFocus={isNew}
            onChangeText={setFront}
          />
          <TextField
            label={t('note.backLabel')}
            placeholder={t('note.backPlaceholder')}
            value={back}
            onChangeText={setBack}
          />
        </View>
      )}

      <View className="gap-2 pb-2">
        <Text variant="overline">{t('note.previewLabel')}</Text>

        <Card className="min-h-36">
          {previewFaces === null ? (
            <Text variant="body" muted>
              {t('note.previewEmpty')}
            </Text>
          ) : (
            <View className="gap-3">
              <Text variant="heading">{previewFaces.question}</Text>
              <View className="h-px bg-border" />
              <Text variant="body" muted>
                {previewFaces.answer}
              </Text>
            </View>
          )}
        </Card>
      </View>
    </Screen>
  );
};
