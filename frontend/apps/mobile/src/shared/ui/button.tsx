import { ActivityIndicator, type PressableProps } from 'react-native';

import { PressableScale } from './pressable-scale';
import { Text } from './text';

const VARIANT_CLASS = {
  primary: 'bg-primary',
  secondary: 'bg-primary-soft',
  ghost: 'bg-transparent',
  danger: 'bg-danger',
} as const;

const LABEL_CLASS = {
  primary: 'text-primary-foreground',
  secondary: 'text-primary',
  ghost: 'text-primary',
  danger: 'text-primary-foreground',
} as const;

const SIZE_CLASS = {
  large: 'h-14 rounded-full px-6',
  medium: 'h-12 rounded-full px-5',
  small: 'h-10 rounded-full px-4',
} as const;

const DISABLED_CLASS = 'bg-border';
const DISABLED_LABEL_CLASS = 'text-muted';

export type ButtonVariant = keyof typeof VARIANT_CLASS;
export type ButtonSize = keyof typeof SIZE_CLASS;

export interface ButtonProps extends Omit<PressableProps, 'children' | 'className' | 'style'> {
  label: string;
  variant?: ButtonVariant;
  size?: ButtonSize;
  isLoading?: boolean;
  fullWidth?: boolean;
}

export const Button = ({
  label,
  variant = 'primary',
  size = 'medium',
  isLoading = false,
  fullWidth = true,
  disabled,
  ...rest
}: ButtonProps) => {
  const isDisabled = disabled === true || isLoading;

  return (
    <PressableScale
      accessibilityRole="button"
      accessibilityState={{ disabled: isDisabled, busy: isLoading }}
      disabled={isDisabled}
      containerClassName={fullWidth ? 'w-full' : ''}
      className={[
        'flex-row items-center justify-center',
        isDisabled ? DISABLED_CLASS : VARIANT_CLASS[variant],
        SIZE_CLASS[size],
        fullWidth ? 'w-full' : '',
      ].join(' ')}
      {...rest}
    >
      {isLoading ? (
        <ActivityIndicator />
      ) : (
        <Text
          variant="body"
          className={[
            'font-semibold tracking-[-0.1px]',
            isDisabled ? DISABLED_LABEL_CLASS : LABEL_CLASS[variant],
          ].join(' ')}
        >
          {label}
        </Text>
      )}
    </PressableScale>
  );
};
