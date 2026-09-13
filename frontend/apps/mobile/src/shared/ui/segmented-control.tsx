import { useEffect, useRef, useState } from 'react';
import { type LayoutChangeEvent, Pressable, StyleSheet, View } from 'react-native';
import Animated, {
  Easing,
  ReduceMotion,
  type SharedValue,
  useAnimatedStyle,
  useSharedValue,
  withTiming,
} from 'react-native-reanimated';

import { Text } from './text';

const TRACK_PADDING = 4;
const SLIDE_DURATION_MS = 240;

export interface SegmentedOption<Value extends string> {
  readonly value: Value;
  readonly label: string;
}

export interface SegmentedControlProps<Value extends string> {
  options: readonly SegmentedOption<Value>[];
  value: Value;
  onChange: (value: Value) => void;
}

interface SegmentProps {
  label: string;
  index: number;
  segmentWidth: number;
  translateX: SharedValue<number>;
  selected: boolean;
  onSelect: () => void;
}

const Segment = ({ label, index, segmentWidth, translateX, selected, onSelect }: SegmentProps) => {
  const overlayStyle = useAnimatedStyle(() => {
    if (segmentWidth <= 0) {
      return { opacity: 0 };
    }

    const distance = Math.abs(translateX.value - index * segmentWidth);

    return { opacity: Math.max(0, Math.min(1, 1 - distance / segmentWidth)) };
  });

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected }}
      onPress={onSelect}
      className="flex-1 items-center justify-center py-2.5"
    >
      <View className="items-center justify-center">
        <Text variant="label" className="text-primary" style={styles.tabularNums}>
          {label}
        </Text>
        <Animated.View
          pointerEvents="none"
          className="absolute inset-0 items-center justify-center"
          style={overlayStyle}
        >
          <Text variant="label" className="text-primary-foreground" style={styles.tabularNums}>
            {label}
          </Text>
        </Animated.View>
      </View>
    </Pressable>
  );
};

export const SegmentedControl = <Value extends string>({
  options,
  value,
  onChange,
}: SegmentedControlProps<Value>) => {
  const [trackWidth, setTrackWidth] = useState(0);
  const translateX = useSharedValue(0);
  const initialized = useRef(false);

  const selectedIndex = Math.max(
    0,
    options.findIndex((option) => option.value === value),
  );
  const segmentWidth = options.length > 0 ? (trackWidth - TRACK_PADDING * 2) / options.length : 0;

  useEffect(() => {
    const target = selectedIndex * segmentWidth;

    if (!initialized.current) {
      translateX.value = target;

      if (trackWidth > 0) {
        initialized.current = true;
      }

      return;
    }

    translateX.value = withTiming(target, {
      duration: SLIDE_DURATION_MS,
      easing: Easing.out(Easing.cubic),
      reduceMotion: ReduceMotion.System,
    });
  }, [segmentWidth, selectedIndex, trackWidth, translateX]);

  const thumbStyle = useAnimatedStyle(() => ({
    transform: [{ translateX: translateX.value }],
  }));

  const handleLayout = (event: LayoutChangeEvent) => {
    setTrackWidth(event.nativeEvent.layout.width);
  };

  return (
    <View onLayout={handleLayout} className="flex-row rounded-2xl bg-primary-soft p-1">
      {segmentWidth > 0 ? (
        <Animated.View
          pointerEvents="none"
          className="absolute rounded-xl bg-primary"
          style={[
            { top: TRACK_PADDING, bottom: TRACK_PADDING, left: TRACK_PADDING, width: segmentWidth },
            thumbStyle,
          ]}
        />
      ) : null}

      {options.map((option, index) => (
        <Segment
          key={option.value}
          label={option.label}
          index={index}
          segmentWidth={segmentWidth}
          translateX={translateX}
          selected={option.value === value}
          onSelect={() => {
            onChange(option.value);
          }}
        />
      ))}
    </View>
  );
};

const styles = StyleSheet.create({
  tabularNums: {
    fontVariant: ['tabular-nums'],
  },
});
