import { View } from 'react-native';

import { Card, SegmentedControl, Text } from '@/shared/ui';
import type { SegmentedOption } from '@/shared/ui';

export interface SettingCardProps<Value extends string> {
  label: string;
  options: readonly SegmentedOption<Value>[];
  value: Value;
  onChange: (value: Value) => void;
}

export const SettingCard = <Value extends string>({
  label,
  options,
  value,
  onChange,
}: SettingCardProps<Value>) => (
  <Card>
    <Text variant="label">{label}</Text>

    <View className="mt-3">
      <SegmentedControl options={options} value={value} onChange={onChange} />
    </View>
  </Card>
);
