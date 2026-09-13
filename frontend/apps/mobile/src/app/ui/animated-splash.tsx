import { useColorScheme } from 'nativewind';
import { useEffect, useState } from 'react';
import { AccessibilityInfo, StyleSheet } from 'react-native';
import Animated, {
  Easing,
  interpolateColor,
  runOnJS,
  useAnimatedStyle,
  useSharedValue,
  withDelay,
  withSequence,
  withSpring,
  withTiming,
} from 'react-native-reanimated';

import { FONT_FAMILY } from '@/shared/config';

const WORDMARK = 'Deckly';
const LETTERS = WORDMARK.split('');

const REVEAL_STAGGER_MS = 110;
const REVEAL_DURATION_MS = 340;
const RISE_DISTANCE = 18;
const REST_OPACITY = 0.9;

const SHIMMER_START_MS = LETTERS.length * REVEAL_STAGGER_MS + 80;
const SHIMMER_STAGGER_MS = 70;
const SHIMMER_UP_MS = 160;
const SHIMMER_DOWN_MS = 260;

const JUMP_START_MS = SHIMMER_START_MS + LETTERS.length * SHIMMER_STAGGER_MS + 260;
const JUMP_STAGGER_MS = 80;
const JUMP_HEIGHT = 24;
const JUMP_SETTLE_MS = 1000;

const FADE_START_MS = JUMP_START_MS + (LETTERS.length - 1) * JUMP_STAGGER_MS + JUMP_SETTLE_MS;
const FADE_DURATION_MS = 520;
const REDUCED_HOLD_MS = 700;

const PULSE_SCALE = 0.18;
const GLOW_RADIUS = 18;

const PALETTE = {
  light: { base: 'rgb(91, 75, 247)', glow: 'rgb(130, 156, 255)' },
  dark: { base: 'rgb(138, 132, 255)', glow: 'rgb(196, 205, 255)' },
} as const;

interface SplashLetterProps {
  char: string;
  index: number;
  base: string;
  glow: string;
  reduceMotion: boolean;
}

const SplashLetter = ({ char, index, base, glow, reduceMotion }: SplashLetterProps) => {
  const reveal = useSharedValue(0);
  const pulse = useSharedValue(0);
  const jump = useSharedValue(0);

  useEffect(() => {
    if (reduceMotion) {
      reveal.value = withTiming(1, { duration: REVEAL_DURATION_MS });
      return;
    }

    reveal.value = withDelay(
      index * REVEAL_STAGGER_MS,
      withTiming(1, { duration: REVEAL_DURATION_MS, easing: Easing.out(Easing.cubic) }),
    );

    pulse.value = withDelay(
      SHIMMER_START_MS + index * SHIMMER_STAGGER_MS,
      withSequence(
        withTiming(1, { duration: SHIMMER_UP_MS, easing: Easing.out(Easing.quad) }),
        withTiming(0, { duration: SHIMMER_DOWN_MS, easing: Easing.in(Easing.quad) }),
      ),
    );

    jump.value = withDelay(
      JUMP_START_MS + index * JUMP_STAGGER_MS,
      withSequence(
        withTiming(-JUMP_HEIGHT, { duration: 200, easing: Easing.out(Easing.quad) }),
        withSpring(0, { damping: 12, stiffness: 240, mass: 0.7 }),
      ),
    );
  }, [index, jump, pulse, reduceMotion, reveal]);

  const style = useAnimatedStyle(() => ({
    opacity: reveal.value * (REST_OPACITY + (1 - REST_OPACITY) * pulse.value),
    color: interpolateColor(pulse.value, [0, 1], [base, glow]),
    textShadowColor: glow,
    textShadowRadius: pulse.value * GLOW_RADIUS,
    transform: [
      { translateY: (1 - reveal.value) * RISE_DISTANCE + jump.value },
      { scale: 1 + pulse.value * PULSE_SCALE },
    ],
  }));

  return (
    <Animated.Text style={[styles.letter, style]} accessible={false}>
      {char}
    </Animated.Text>
  );
};

export const AnimatedSplash = () => {
  const { colorScheme } = useColorScheme();
  const overlay = useSharedValue(1);
  const [visible, setVisible] = useState(true);
  const [reduceMotion, setReduceMotion] = useState<boolean | null>(null);

  useEffect(() => {
    let active = true;

    void AccessibilityInfo.isReduceMotionEnabled().then((enabled) => {
      if (active) {
        setReduceMotion(enabled);
      }
    });

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (reduceMotion === null) {
      return;
    }

    const startAt = reduceMotion ? REDUCED_HOLD_MS : FADE_START_MS;

    overlay.value = withDelay(
      startAt,
      withTiming(0, { duration: FADE_DURATION_MS, easing: Easing.in(Easing.cubic) }, (finished) => {
        if (finished === true) {
          runOnJS(setVisible)(false);
        }
      }),
    );
  }, [overlay, reduceMotion]);

  const overlayStyle = useAnimatedStyle(() => ({ opacity: overlay.value }));

  if (!visible) {
    return null;
  }

  const palette = colorScheme === 'dark' ? PALETTE.dark : PALETTE.light;

  return (
    <Animated.View
      pointerEvents="auto"
      style={[StyleSheet.absoluteFill, styles.overlay, overlayStyle]}
    >
      <Animated.View
        className="flex-1 items-center justify-center bg-background"
        accessibilityRole="image"
        accessibilityLabel={WORDMARK}
      >
        <Animated.View className="flex-row">
          {reduceMotion === null
            ? null
            : LETTERS.map((char, index) => (
                <SplashLetter
                  key={`${char}-${index}`}
                  char={char}
                  index={index}
                  base={palette.base}
                  glow={palette.glow}
                  reduceMotion={reduceMotion}
                />
              ))}
        </Animated.View>
      </Animated.View>
    </Animated.View>
  );
};

const styles = StyleSheet.create({
  overlay: {
    zIndex: 50,
  },
  letter: {
    fontFamily: FONT_FAMILY.display,
    fontSize: 56,
    fontWeight: '800',
    letterSpacing: -1.5,
  },
});
