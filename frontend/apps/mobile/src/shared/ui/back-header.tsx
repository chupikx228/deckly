import type { ReactNode } from 'react';
import { View } from 'react-native';

import { IconButton } from './icon-button';

export interface BackHeaderProps {
  onBack: () => void;
  accessibilityLabel: string;
  action?: ReactNode;
}

export const BackHeader = ({ onBack, accessibilityLabel, action }: BackHeaderProps) => (
  <View className="-mx-2 h-12 flex-row items-center justify-between">
    <IconButton
      name="chevron-back"
      size={26}
      accessibilityLabel={accessibilityLabel}
      onPress={onBack}
    />

    {action ?? null}
  </View>
);
