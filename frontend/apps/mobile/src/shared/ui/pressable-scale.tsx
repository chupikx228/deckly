import type { ReactNode } from 'react';
import { Pressable, type PressableProps, type StyleProp, type ViewStyle } from 'react-native';
import Animated, {
  ReduceMotion,
  useAnimatedStyle,
  useSharedValue,
  withTiming,
} from 'react-native-reanimated';

const PRESSED_SCALE = 0.97;
const PRESSED_OPACITY = 0.9;
const PRESS_DURATION_MS = 120;

export interface PressableScaleProps extends Omit<PressableProps, 'children' | 'style'> {
  children: ReactNode;
  className?: string;
  containerClassName?: string;
  style?: StyleProp<ViewStyle>;
}

export const PressableScale = ({
  children,
  className = '',
  containerClassName = '',
  style,
  onPressIn,
  onPressOut,
  ...rest
}: PressableScaleProps) => {
  const active = useSharedValue(0);

  const animatedStyle = useAnimatedStyle(() => ({
    transform: [{ scale: 1 - active.value * (1 - PRESSED_SCALE) }],
    opacity: 1 - active.value * (1 - PRESSED_OPACITY),
  }));

  return (
    <Pressable
      className={containerClassName}
      onPressIn={(event) => {
        active.value = withTiming(1, {
          duration: PRESS_DURATION_MS,
          reduceMotion: ReduceMotion.System,
        });
        onPressIn?.(event);
      }}
      onPressOut={(event) => {
        active.value = withTiming(0, {
          duration: PRESS_DURATION_MS,
          reduceMotion: ReduceMotion.System,
        });
        onPressOut?.(event);
      }}
      {...rest}
    >
      <Animated.View className={className} style={[style, animatedStyle]}>
        {children}
      </Animated.View>
    </Pressable>
  );
};
