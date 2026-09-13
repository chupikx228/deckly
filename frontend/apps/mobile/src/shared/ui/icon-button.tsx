import type { ComponentProps } from 'react';
import { Pressable } from 'react-native';

import { Icon } from './icon';

const TONE_CLASS = {
  default: 'text-foreground',
  primary: 'text-primary',
  danger: 'text-danger',
} as const;

export type IconButtonTone = keyof typeof TONE_CLASS;

export interface IconButtonProps {
  name: ComponentProps<typeof Icon>['name'];
  accessibilityLabel: string;
  onPress: () => void;
  tone?: IconButtonTone;
  size?: number;
}

export const IconButton = ({
  name,
  accessibilityLabel,
  onPress,
  tone = 'default',
  size = 24,
}: IconButtonProps) => (
  <Pressable
    accessibilityRole="button"
    accessibilityLabel={accessibilityLabel}
    hitSlop={12}
    onPress={onPress}
    className="h-11 w-11 items-center justify-center rounded-full active:bg-primary-soft"
  >
    <Icon name={name} size={size} className={TONE_CLASS[tone]} />
  </Pressable>
);
