import { useTranslation } from 'react-i18next';
import { View } from 'react-native';

import { PressableScale, Text } from '@/shared/ui';

import {
  GENERATABLE_TYPES,
  NOTE_TYPE_LABEL_KEY,
  type GeneratableType,
} from '../model/wizard-options';

export interface NoteTypePickerProps {
  selected: readonly GeneratableType[];
  onToggle: (type: GeneratableType) => void;
}

export const NoteTypePicker = ({ selected, onToggle }: NoteTypePickerProps) => {
  const { t } = useTranslation('generation');

  return (
    <View className="flex-row flex-wrap gap-2">
      {GENERATABLE_TYPES.map((type) => {
        const isSelected = selected.includes(type);

        return (
          <PressableScale
            key={type}
            accessibilityRole="button"
            accessibilityState={{ selected: isSelected }}
            onPress={() => {
              onToggle(type);
            }}
            className={[
              'rounded-full border px-4 py-2.5',
              isSelected ? 'border-primary bg-primary-soft' : 'border-border bg-surface',
            ].join(' ')}
          >
            <Text variant="label" className={isSelected ? 'text-primary' : 'text-foreground'}>
              {t(NOTE_TYPE_LABEL_KEY[type])}
            </Text>
          </PressableScale>
        );
      })}
    </View>
  );
};
