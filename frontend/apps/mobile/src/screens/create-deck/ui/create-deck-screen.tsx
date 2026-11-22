import { useRouter } from 'expo-router';
import { useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { getRequestErrorMessageKey } from '@/entities/generation';
import { describeError } from '@/shared/api';
import { routes } from '@/shared/config';
import { Button, Screen, SegmentedControl, Text, TextField } from '@/shared/ui';

import { useGenerationForm } from '../model/use-generation-form';
import {
  CARD_COUNT_OPTIONS,
  CONTENT_LANGUAGE_LABEL,
  CONTENT_LANGUAGE_OPTIONS,
  DIFFICULTY_LABEL_KEY,
  DIFFICULTY_OPTIONS,
  GENERATION_SOURCE,
  GENERATION_SOURCE_LABEL_KEY,
  GENERATION_SOURCE_OPTIONS,
} from '../model/wizard-options';
import { NoteTypePicker } from './note-type-picker';
import { ServiceBanner } from './service-banner';

export const CreateDeckScreen = () => {
  const { t } = useTranslation('generation');
  const { t: tErrors } = useTranslation('errors');
  const router = useRouter();

  const form = useGenerationForm();

  const sourceOptions = useMemo(
    () =>
      GENERATION_SOURCE_OPTIONS.map((value) => ({
        value,
        label: t(GENERATION_SOURCE_LABEL_KEY[value]),
      })),
    [t],
  );

  const countOptions = useMemo(
    () => CARD_COUNT_OPTIONS.map((count) => ({ value: String(count), label: String(count) })),
    [],
  );

  const difficultyOptions = useMemo(
    () => DIFFICULTY_OPTIONS.map((value) => ({ value, label: t(DIFFICULTY_LABEL_KEY[value]) })),
    [t],
  );

  const languageOptions = useMemo(
    () =>
      CONTENT_LANGUAGE_OPTIONS.map((value) => ({ value, label: CONTENT_LANGUAGE_LABEL[value] })),
    [],
  );

  const handleSubmit = useCallback(() => {
    form.submit((jobId) => {
      router.push(routes.generation(jobId));
    });
  }, [form, router]);

  return (
    <Screen scrollable>
      <Text variant="display">{t('wizard.title')}</Text>

      <ServiceBanner />

      <SegmentedControl options={sourceOptions} value={form.source} onChange={form.setSource} />

      {form.source === GENERATION_SOURCE.DOCUMENT ? (
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
      ) : (
        <TextField
          placeholder={t('wizard.topicPlaceholder')}
          value={form.topic}
          multiline
          onChangeText={form.setTopic}
        />
      )}

      <View className="gap-2">
        <Text variant="overline">{t('wizard.cardCount')}</Text>
        <SegmentedControl
          options={countOptions}
          value={form.cardCount}
          onChange={form.setCardCount}
        />
      </View>

      <View className="gap-2">
        <Text variant="overline">{t('wizard.difficulty')}</Text>
        <SegmentedControl
          options={difficultyOptions}
          value={form.difficulty}
          onChange={form.setDifficulty}
        />
      </View>

      <View className="gap-2">
        <Text variant="overline">{t('wizard.noteTypes')}</Text>
        <NoteTypePicker selected={form.noteTypes} onToggle={form.toggleNoteType} />
      </View>

      <View className="gap-2">
        <Text variant="overline">{t('wizard.contentLanguage')}</Text>
        <SegmentedControl
          options={languageOptions}
          value={form.contentLanguage}
          onChange={form.setContentLanguage}
        />
      </View>

      <TextField
        label={t('wizard.instructionsLabel')}
        placeholder={t('wizard.instructionsPlaceholder')}
        value={form.instructions}
        multiline
        onChangeText={form.setInstructions}
      />

      {form.isUploading ? (
        <Text variant="caption">{t('wizard.uploading', { percent: form.uploadPercent })}</Text>
      ) : null}

      {form.error === null ? null : (
        <Text variant="caption" className="text-danger">
          {tErrors(getRequestErrorMessageKey(describeError(form.error)))}
        </Text>
      )}

      <Button
        label={t('wizard.submit')}
        disabled={!form.canSubmit}
        isLoading={form.isSubmitting}
        onPress={handleSubmit}
      />
    </Screen>
  );
};
