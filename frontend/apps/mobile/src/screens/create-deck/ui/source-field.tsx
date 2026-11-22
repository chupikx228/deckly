import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { Button, Text, TextField } from '@/shared/ui';

import type { GenerationForm } from '../model/use-generation-form';
import { GENERATION_SOURCE } from '../model/wizard-options';

export interface SourceFieldProps {
  form: GenerationForm;
}

export const SourceField = ({ form }: SourceFieldProps) => {
  const { t } = useTranslation('generation');

  if (form.source === GENERATION_SOURCE.DOCUMENT) {
    return (
      <View className="gap-2">
        {form.file === null ? null : (
          <Text variant="label" numberOfLines={1}>
            {form.file.name}
          </Text>
        )}

        <Button
          label={form.file === null ? t('wizard.chooseFile') : t('wizard.replaceFile')}
          variant="secondary"
          onPress={form.pickFile}
        />
      </View>
    );
  }

  if (form.source === GENERATION_SOURCE.ARTICLE) {
    return (
      <TextField
        placeholder={t('wizard.urlPlaceholder')}
        value={form.articleUrl}
        autoCapitalize="none"
        autoCorrect={false}
        keyboardType="url"
        onChangeText={form.setArticleUrl}
      />
    );
  }

  return (
    <TextField
      placeholder={t('wizard.topicPlaceholder')}
      value={form.topic}
      multiline
      onChangeText={form.setTopic}
    />
  );
};
