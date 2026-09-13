import { View } from 'react-native';

import { Button } from './button';
import { Text } from './text';

export interface EmptyStateProps {
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
}

export const EmptyState = ({ title, description, actionLabel, onAction }: EmptyStateProps) => (
  <View className="flex-1 items-center justify-center gap-2">
    <Text variant="heading" className="text-center">
      {title}
    </Text>

    <Text variant="body" muted className="text-center">
      {description}
    </Text>

    {actionLabel !== undefined && onAction !== undefined ? (
      <View className="mt-4 w-full">
        <Button label={actionLabel} onPress={onAction} />
      </View>
    ) : null}
  </View>
);
