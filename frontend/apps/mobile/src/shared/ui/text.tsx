import { Text as RNText, type TextProps as RNTextProps } from 'react-native';

import { FONT_FAMILY } from '@/shared/config';

const VARIANT_CLASS = {
  display: 'text-display font-extrabold text-foreground',
  title: 'text-title font-bold text-foreground',
  heading: 'text-heading font-semibold text-foreground',
  body: 'text-body text-foreground',
  label: 'text-label font-medium text-foreground',
  overline: 'text-caption font-semibold uppercase tracking-[0.6px] text-muted',
  caption: 'text-caption text-muted',
} as const;

export type TextVariant = keyof typeof VARIANT_CLASS;

export interface TextProps extends RNTextProps {
  variant?: TextVariant;
  muted?: boolean;
}

export const Text = ({ variant = 'body', muted = false, className, style, ...rest }: TextProps) => (
  <RNText
    className={[VARIANT_CLASS[variant], muted ? 'text-muted' : '', className ?? ''].join(' ')}
    style={[variant === 'display' ? { fontFamily: FONT_FAMILY.display } : null, style]}
    {...rest}
  />
);
