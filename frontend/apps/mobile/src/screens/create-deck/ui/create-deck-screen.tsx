import { useRouter } from 'expo-router';
import { useCallback, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { useCreateGeneration } from '@/features/generate-deck';
import {
  DIFFICULTY,
  GENERATION,
  NOTE_TYPE,
  routes,
  type Difficulty,
  type Locale,
} from '@/shared/config';
import { useLocale } from '@/shared/lib';
import { Button, Screen, SegmentedControl, Text, TextField } from '@/shared/ui';

import {
  CARD_COUNT_OPTIONS,
  CONTENT_LANGUAGE_LABEL,
  CONTENT_LANGUAGE_OPTIONS,
  DIFFICULTY_LABEL_KEY,
  DIFFICULTY_OPTIONS,
  type GeneratableType,
} from '../model/wizard-options';
import { NoteTypePicker } from './note-type-picker';

const MIN_TOPIC_LENGTH = 3;

export const CreateDeckScreen = () => {
  const { t } = useTranslation('generation');
  const { t: tErrors } = useTranslation('errors');
  const router = useRouter();
  const locale = useLocale();

  const [topic, setTopic] = useState('');
  const [cardCount, setCardCount] = useState<string>(String(GENERATION.DEFAULT_CARD_COUNT));
  const [difficulty, setDifficulty] = useState<Difficulty>(DIFFICULTY.INTERMEDIATE);
  const [noteTypes, setNoteTypes] = useState<readonly GeneratableType[]>([NOTE_TYPE.BASIC]);
  const [contentLanguage, setContentLanguage] = useState<Locale>(locale);
  const [instructions, setInstructions] = useState('');

  const createGeneration = useCreateGeneration();

  const canSubmit =
    topic.trim().length >= MIN_TOPIC_LENGTH && noteTypes.length > 0 && !createGeneration.isPending;

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

  const handleToggleType = useCallback((type: GeneratableType) => {
    setNoteTypes((current) =>
      current.includes(type) ? current.filter((item) => item !== type) : [...current, type],
    );
  }, []);

  const handleSubmit = useCallback(() => {
    if (!canSubmit) {
      return;
    }

    createGeneration.mutate(
      {
        topic: topic.trim(),
        language: contentLanguage,
        cardCount: Number(cardCount),
        difficulty,
        noteTypes: [...noteTypes],
        includeImages: false,
        ...(instructions.trim().length > 0 ? { instructions: instructions.trim() } : {}),
      },
      {
        onSuccess: (job) => {
          router.push(routes.generation(job.jobId));
        },
      },
    );
  }, [
    canSubmit,
    cardCount,
    contentLanguage,
    createGeneration,
    difficulty,
    instructions,
    noteTypes,
    router,
    topic,
  ]);

  return (
    <Screen scrollable>
      <Text variant="display">{t('wizard.title')}</Text>

      <TextField
        placeholder={t('wizard.topicPlaceholder')}
        value={topic}
        multiline
        onChangeText={setTopic}
      />

      <View className="gap-2">
        <Text variant="overline">{t('wizard.cardCount')}</Text>
        <SegmentedControl options={countOptions} value={cardCount} onChange={setCardCount} />
      </View>

      <View className="gap-2">
        <Text variant="overline">{t('wizard.difficulty')}</Text>
        <SegmentedControl options={difficultyOptions} value={difficulty} onChange={setDifficulty} />
      </View>

      <View className="gap-2">
        <Text variant="overline">{t('wizard.noteTypes')}</Text>
        <NoteTypePicker selected={noteTypes} onToggle={handleToggleType} />
      </View>

      <View className="gap-2">
        <Text variant="overline">{t('wizard.contentLanguage')}</Text>
        <SegmentedControl
          options={languageOptions}
          value={contentLanguage}
          onChange={setContentLanguage}
        />
      </View>

      <TextField
        label={t('wizard.instructionsLabel')}
        placeholder={t('wizard.instructionsPlaceholder')}
        value={instructions}
        multiline
        onChangeText={setInstructions}
      />

      {createGeneration.isError ? (
        <Text variant="caption" className="text-danger">
          {tErrors('generic.description')}
        </Text>
      ) : null}

      <Button
        label={t('wizard.submit')}
        disabled={!canSubmit}
        isLoading={createGeneration.isPending}
        onPress={handleSubmit}
      />
    </Screen>
  );
};
