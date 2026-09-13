import type { ReactNode } from 'react';
import { StyleSheet, View } from 'react-native';

import { PressableScale } from './pressable-scale';

export interface CardProps {
  children: ReactNode;
  onPress?: (() => void) | undefined;
  className?: string | undefined;
}

const BASE_CLASS = 'rounded-card bg-surface p-5';

export const Card = ({ children, onPress, className }: CardProps) => {
  if (onPress === undefined) {
    return (
      <View className={[BASE_CLASS, className ?? ''].join(' ')} style={styles.shadow}>
        {children}
      </View>
    );
  }

  return (
    <PressableScale
      accessibilityRole="button"
      onPress={onPress}
      className={[BASE_CLASS, className ?? ''].join(' ')}
      style={styles.shadow}
    >
      {children}
    </PressableScale>
  );
};

const styles = StyleSheet.create({
  shadow: {
    shadowColor: '#0B0D12',
    shadowOpacity: 0.05,
    shadowRadius: 14,
    shadowOffset: { width: 0, height: 2 },
    elevation: 1,
  },
});
