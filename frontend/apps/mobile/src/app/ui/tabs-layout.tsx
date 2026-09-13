import { Ionicons } from '@expo/vector-icons';
import { Tabs } from 'expo-router';
import { useColorScheme } from 'nativewind';
import type { ComponentProps } from 'react';
import { useTranslation } from 'react-i18next';

import { SCREEN, TAB_BAR_TINT, TABS, type TabIconName, type TabScreenId } from '@/shared/config';

type IoniconName = ComponentProps<typeof Ionicons>['name'];

const TAB_ICON: Readonly<Record<TabIconName, IoniconName>> = {
  home: 'home-outline',
  sparkles: 'sparkles-outline',
  layers: 'albums-outline',
  user: 'person-outline',
};

const TAB_TITLE_KEY = {
  [SCREEN.HOME]: 'tabs.home',
  [SCREEN.CREATE]: 'tabs.create',
  [SCREEN.DECKS]: 'tabs.decks',
  [SCREEN.PROFILE]: 'tabs.profile',
} as const satisfies Record<TabScreenId, string>;

export const TabsLayout = () => {
  const { t } = useTranslation('navigation');
  const { colorScheme } = useColorScheme();
  const tint = colorScheme === 'dark' ? TAB_BAR_TINT.dark : TAB_BAR_TINT.light;

  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: tint.active,
        tabBarInactiveTintColor: tint.inactive,
      }}
    >
      {TABS.map((tab) => (
        <Tabs.Screen
          key={tab.id}
          name={tab.routeName}
          options={{
            title: t(TAB_TITLE_KEY[tab.id]),
            tabBarIcon: ({ color, size }) => (
              <Ionicons name={TAB_ICON[tab.icon]} color={color} size={size} />
            ),
          }}
        />
      ))}
    </Tabs>
  );
};
