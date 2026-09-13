import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { createDeck } from '@/features/edit-deck';
import { Button, Card, TextField } from '@/shared/ui';

export interface DeckComposerProps {
  onCreated: (deckId: string) => void;
  onCancel: () => void;
}

export const DeckComposer = ({ onCreated, onCancel }: DeckComposerProps) => {
  const { t } = useTranslation('deck');
  const { t: tCommon } = useTranslation('common');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');

  const canSubmit = title.trim().length > 0;

  const handleCreate = useCallback(() => {
    if (!canSubmit) {
      return;
    }

    onCreated(createDeck({ title, description: description.trim() || null }));
  }, [canSubmit, description, onCreated, title]);

  return (
    <Card>
      <View className="gap-3">
        <TextField
          label={t('form.titleLabel')}
          placeholder={t('form.titlePlaceholder')}
          value={title}
          autoFocus
          onChangeText={setTitle}
        />

        <TextField
          label={t('form.descriptionLabel')}
          placeholder={t('form.descriptionPlaceholder')}
          value={description}
          onChangeText={setDescription}
        />

        <View className="flex-row gap-3">
          <View className="flex-1">
            <Button label={tCommon('actions.cancel')} variant="ghost" onPress={onCancel} />
          </View>
          <View className="flex-1">
            <Button label={t('form.create')} disabled={!canSubmit} onPress={handleCreate} />
          </View>
        </View>
      </View>
    </Card>
  );
};
