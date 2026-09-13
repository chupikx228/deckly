import { Pressable, View } from 'react-native';

import { Icon } from './icon';
import { Text } from './text';

export interface OptionItem<Value extends string> {
  readonly value: Value;
  readonly label: string;
  readonly description?: string | undefined;
}

export interface OptionListProps<Value extends string> {
  options: readonly OptionItem<Value>[];
  value: Value;
  onChange: (value: Value) => void;
}

export const OptionList = <Value extends string>({
  options,
  value,
  onChange,
}: OptionListProps<Value>) => (
  <View className="overflow-hidden rounded-2xl border border-border">
    {options.map((option, index) => {
      const isSelected = option.value === value;

      return (
        <Pressable
          key={option.value}
          accessibilityRole="radio"
          accessibilityState={{ selected: isSelected }}
          onPress={() => {
            onChange(option.value);
          }}
          className={[
            'flex-row items-center gap-3 px-4 py-3.5',
            isSelected ? 'bg-primary-soft' : 'bg-surface',
            index === 0 ? '' : 'border-t border-border',
          ].join(' ')}
        >
          <View className="flex-1 gap-0.5">
            <Text variant="label" className={isSelected ? 'text-primary' : 'text-foreground'}>
              {option.label}
            </Text>

            {option.description === undefined ? null : (
              <Text variant="caption">{option.description}</Text>
            )}
          </View>

          {isSelected ? (
            <Icon name="checkmark-circle" size={22} className="text-primary" />
          ) : (
            <View className="h-5 w-5 rounded-full border-2 border-border" />
          )}
        </Pressable>
      );
    })}
  </View>
);
