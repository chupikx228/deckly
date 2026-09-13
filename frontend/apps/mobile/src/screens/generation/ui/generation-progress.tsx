import type { GenerationJob } from '@deckly/api-contract';
import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { STAGE_LABEL_KEY } from '@/entities/generation';
import { BackHeader, Button, Screen, Text } from '@/shared/ui';

export interface GenerationProgressProps {
  job: GenerationJob;
  isCancelling: boolean;
  onCancel: () => void;
  onBack: () => void;
}

export const GenerationProgress = ({
  job,
  isCancelling,
  onCancel,
  onBack,
}: GenerationProgressProps) => {
  const { t } = useTranslation('generation');
  const { t: tCommon } = useTranslation('common');
  const percent = Math.round(job.progress * 100);

  return (
    <Screen header={<BackHeader onBack={onBack} accessibilityLabel={tCommon('actions.back')} />}>
      <View className="flex-1 justify-center gap-6">
        <View className="gap-2">
          <Text variant="title">
            {job.stage === null ? t('stage.finalizing') : t(STAGE_LABEL_KEY[job.stage])}
          </Text>
          <Text variant="caption">{`${percent}%`}</Text>
        </View>

        <View className="h-2 w-full overflow-hidden rounded-full bg-primary-soft">
          <View className="h-full rounded-full bg-primary" style={{ width: `${percent}%` }} />
        </View>
      </View>

      <View className="pb-6">
        <Button
          label={t('cancel.action')}
          variant="ghost"
          isLoading={isCancelling}
          onPress={onCancel}
        />
      </View>
    </Screen>
  );
};
