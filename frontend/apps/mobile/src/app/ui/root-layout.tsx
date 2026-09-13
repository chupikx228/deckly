import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';

import { SCREEN, SCREEN_REGISTRY, toStackRouteName, type ScreenId } from '@/shared/config';

import { AppProviders } from '../providers';
import { AnimatedSplash } from './animated-splash';

const MODAL_SCREENS: readonly ScreenId[] = [
  SCREEN.NOTE_EDITOR,
  SCREEN.GENERATION,
  SCREEN.STUDY,
  SCREEN.SESSION_SUMMARY,
];

export const RootLayout = () => (
  <AppProviders>
    <StatusBar style="auto" />
    <Stack screenOptions={{ headerShown: false, contentStyle: { flex: 1 } }}>
      <Stack.Screen name="(tabs)" />
      {MODAL_SCREENS.map((id) => (
        <Stack.Screen
          key={id}
          name={toStackRouteName(SCREEN_REGISTRY[id].pattern)}
          options={{ presentation: SCREEN_REGISTRY[id].presentation }}
        />
      ))}
    </Stack>
    <AnimatedSplash />
  </AppProviders>
);
