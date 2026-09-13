import { getLocales } from 'expo-localization';
import { createInstance, type i18n as I18nInstance } from 'i18next';
import { initReactI18next } from 'react-i18next';

import {
  DEFAULT_LOCALE,
  DEFAULT_NAMESPACE,
  I18N_NAMESPACES,
  resolveLocale,
  type Locale,
} from '@/shared/config';

import { resources } from './resources';

export const detectDeviceLocale = (): Locale =>
  resolveLocale(getLocales().map((locale) => locale.languageTag));

export const i18n: I18nInstance = createInstance();

export const initI18n = (locale: Locale | null): I18nInstance => {
  if (i18n.isInitialized) {
    return i18n;
  }

  void i18n.use(initReactI18next).init({
    resources,
    lng: locale ?? detectDeviceLocale(),
    fallbackLng: DEFAULT_LOCALE,
    defaultNS: DEFAULT_NAMESPACE,
    ns: I18N_NAMESPACES,
    interpolation: { escapeValue: false },
    returnNull: false,
  });

  return i18n;
};

export const changeLocale = async (locale: Locale): Promise<void> => {
  await i18n.changeLanguage(locale);
};
