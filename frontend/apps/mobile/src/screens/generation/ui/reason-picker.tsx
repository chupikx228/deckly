import type { RejectionReason } from '@deckly/api-contract';
import { useTranslation } from 'react-i18next';
import { Pressable, View } from 'react-native';

import { Button, Text } from '@/shared/ui';

import { REJECTION_REASON_LABEL_KEY, REJECTION_REASON_OPTIONS } from '../model/rejection-options';

export interface ReasonPickerProps {
  onPick: (reason: RejectionReason) => void;
  onCancel: () => void;
}

export const ReasonPicker = ({ onPick, onCancel }: ReasonPickerProps) => {
  const { t } = useTranslation('generation');
  const { t: tCommon } = useTranslation('common');

  return (
    <View className="gap-2">
      <Text variant="overline">{t('rejection.prompt')}</Text>

      <View className="overflow-hidden rounded-2xl border border-border">
        {REJECTION_REASON_OPTIONS.map((reason, index) => (
          <Pressable
            key={reason}
            accessibilityRole="button"
            onPress={() => {
              onPick(reason);
            }}
            className={['bg-surface px-4 py-3', index === 0 ? '' : 'border-t border-border'].join(
              ' ',
            )}
          >
            <Text variant="label">{t(REJECTION_REASON_LABEL_KEY[reason])}</Text>
          </Pressable>
        ))}
      </View>

      <Button label={tCommon('actions.cancel')} variant="ghost" size="small" onPress={onCancel} />
    </View>
  );
};
