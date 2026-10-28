import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { Button, EmptyState, Screen } from '@/shared/ui';

export interface GenerationLongWaitProps {
  isCancelling: boolean;
  onCancel: () => void;
  onKeepWaiting: () => void;
}

export const GenerationLongWait = ({
  isCancelling,
  onCancel,
  onKeepWaiting,
}: GenerationLongWaitProps) => {
  const { t } = useTranslation('generation');
  const { t: tErrors } = useTranslation('errors');

  return (
    <Screen
      footer={
        <View className="gap-2">
          <Button
            label={t('cancel.action')}
            variant="danger"
            isLoading={isCancelling}
            onPress={onCancel}
          />
          <Button label={t('timeout.keepWaiting')} variant="ghost" onPress={onKeepWaiting} />
        </View>
      }
    >
      <EmptyState
        title={tErrors('generation.longWaitTitle')}
        description={tErrors('generation.timeout')}
      />
    </Screen>
  );
};
