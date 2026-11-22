import { useRouter } from 'expo-router';
import { useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';

import { LOCALE_LABEL } from '@/shared/config';
import { useLocale } from '@/shared/lib';
import { useSettingsStore } from '@/shared/model';
import { BackHeader, Screen, Text } from '@/shared/ui';

import { LANGUAGE_OPTIONS, THEME_LABEL_KEY, THEME_OPTIONS } from '../model/settings-options';
import { SettingCard } from './setting-card';

export const SettingsScreen = () => {
  const { t } = useTranslation('profile');
  const { t: tCommon } = useTranslation('common');
  const router = useRouter();

  const theme = useSettingsStore((state) => state.theme);
  const locale = useLocale();
  const storedContentLanguage = useSettingsStore((state) => state.contentLanguage);
  const setTheme = useSettingsStore((state) => state.setTheme);
  const setLocale = useSettingsStore((state) => state.setLocale);
  const setContentLanguage = useSettingsStore((state) => state.setContentLanguage);

  const goBack = useCallback(() => {
    router.back();
  }, [router]);

  const themeOptions = useMemo(
    () => THEME_OPTIONS.map((value) => ({ value, label: t(THEME_LABEL_KEY[value]) })),
    [t],
  );

  const languageOptions = useMemo(
    () => LANGUAGE_OPTIONS.map((value) => ({ value, label: LOCALE_LABEL[value] })),
    [],
  );

  return (
    <Screen
      scrollable
      header={<BackHeader onBack={goBack} accessibilityLabel={tCommon('actions.back')} />}
    >
      <Text variant="display">{t('settings.title')}</Text>

      <SettingCard
        label={t('settings.theme')}
        options={themeOptions}
        value={theme}
        onChange={setTheme}
      />

      <SettingCard
        label={t('settings.language')}
        options={languageOptions}
        value={locale}
        onChange={setLocale}
      />

      <SettingCard
        label={t('settings.contentLanguage')}
        options={languageOptions}
        value={storedContentLanguage ?? locale}
        onChange={setContentLanguage}
      />
    </Screen>
  );
};
