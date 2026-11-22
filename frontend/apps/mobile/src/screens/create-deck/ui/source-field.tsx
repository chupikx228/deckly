import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { Button, Text, TextField } from '@/shared/ui';

import type { GenerationForm } from '../model/use-generation-form';
import { GENERATION_SOURCE } from '../model/wizard-options';

const FILE_SOURCES = [GENERATION_SOURCE.DOCUMENT, GENERATION_SOURCE.RECORDING] as const;

const isFileSource = (form: GenerationForm): boolean =>
  FILE_SOURCES.some((source) => source === form.source);

export interface SourceFieldProps {
  form: GenerationForm;
}

export const SourceField = ({ form }: SourceFieldProps) => {
  const { t } = useTranslation('generation');

  if (isFileSource(form)) {
    const isRecording = form.source === GENERATION_SOURCE.RECORDING;

    const chooseKey = isRecording ? 'wizard.chooseRecording' : 'wizard.chooseFile';
    const replaceKey = isRecording ? 'wizard.replaceRecording' : 'wizard.replaceFile';

    return (
      <View className="gap-2">
        {form.file === null ? null : (
          <Text variant="label" numberOfLines={1}>
            {form.file.name}
          </Text>
        )}

        <Button
          label={t(form.file === null ? chooseKey : replaceKey)}
          variant="secondary"
          onPress={form.pickFile}
        />
      </View>
    );
  }

  if (form.source === GENERATION_SOURCE.ARTICLE) {
    return (
      <View className="gap-1.5">
        <TextField
          placeholder={t('wizard.urlPlaceholder')}
          value={form.articleUrl}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          onChangeText={form.setArticleUrl}
        />

        <Text variant="caption">{t('wizard.urlHint')}</Text>
      </View>
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
