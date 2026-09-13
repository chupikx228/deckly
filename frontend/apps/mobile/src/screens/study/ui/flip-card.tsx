import { Pressable, ScrollView } from 'react-native';
import Animated, {
  interpolate,
  useAnimatedStyle,
  useDerivedValue,
  withTiming,
} from 'react-native-reanimated';

import { Text } from '@/shared/ui';

const FLIP_DURATION_MS = 340;
const PERSPECTIVE = 1200;

export interface FlipCardProps {
  question: string;
  answer: string;
  hint: string | null;
  isRevealed: boolean;
  revealLabel: string;
  onReveal: () => void;
}

const FACE_CLASS =
  'absolute inset-0 items-center justify-center rounded-card border border-border bg-surface p-6';

export const FlipCard = ({
  question,
  answer,
  hint,
  isRevealed,
  revealLabel,
  onReveal,
}: FlipCardProps) => {
  const progress = useDerivedValue(() =>
    withTiming(isRevealed ? 1 : 0, { duration: FLIP_DURATION_MS }),
  );

  const frontStyle = useAnimatedStyle(() => ({
    backfaceVisibility: 'hidden',
    opacity: progress.value < 0.5 ? 1 : 0,
    transform: [
      { perspective: PERSPECTIVE },
      { rotateY: `${interpolate(progress.value, [0, 1], [0, 180])}deg` },
    ],
  }));

  const backStyle = useAnimatedStyle(() => ({
    backfaceVisibility: 'hidden',
    opacity: progress.value < 0.5 ? 0 : 1,
    transform: [
      { perspective: PERSPECTIVE },
      { rotateY: `${interpolate(progress.value, [0, 1], [180, 360])}deg` },
    ],
  }));

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={isRevealed ? answer : revealLabel}
      disabled={isRevealed}
      onPress={onReveal}
      className="flex-1"
    >
      <Animated.View className={FACE_CLASS} style={frontStyle}>
        <ScrollView
          className="w-full"
          contentContainerClassName="grow items-center justify-center"
          showsVerticalScrollIndicator={false}
        >
          <Text variant="title" className="text-center">
            {question}
          </Text>
        </ScrollView>

        <Text variant="caption" className="mt-4">
          {revealLabel}
        </Text>
      </Animated.View>

      <Animated.View className={FACE_CLASS} style={backStyle}>
        <ScrollView
          className="w-full"
          contentContainerClassName="grow items-center justify-center gap-3"
          showsVerticalScrollIndicator={false}
        >
          <Text variant="title" className="text-center">
            {answer}
          </Text>

          {hint === null ? null : (
            <Text variant="body" muted className="text-center">
              {hint}
            </Text>
          )}
        </ScrollView>
      </Animated.View>
    </Pressable>
  );
};
