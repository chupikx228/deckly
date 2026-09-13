import { TextInput, View, type TextInputProps } from 'react-native';

import { Text } from './text';

export interface TextFieldProps extends Omit<TextInputProps, 'className'> {
  label?: string;
  error?: string;
  multiline?: boolean;
}

export const TextField = ({ label, error, multiline = false, ...rest }: TextFieldProps) => (
  <View className="gap-1.5">
    {label !== undefined ? <Text variant="overline">{label}</Text> : null}

    <TextInput
      className={[
        'rounded-2xl border bg-surface px-4 py-3 text-body text-foreground',
        multiline ? 'min-h-28' : 'min-h-12',
        error === undefined ? 'border-border' : 'border-danger',
      ].join(' ')}
      placeholderClassName="text-muted"
      multiline={multiline}
      textAlignVertical={multiline ? 'top' : 'center'}
      {...rest}
    />

    {error !== undefined ? (
      <Text variant="caption" className="text-danger">
        {error}
      </Text>
    ) : null}
  </View>
);
