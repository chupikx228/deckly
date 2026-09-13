import { MMKV } from 'react-native-mmkv';
import type { StateStorage } from 'zustand/middleware';

export const STORAGE_KEY = {
  SETTINGS: 'settings',
} as const;

export type StorageKey = (typeof STORAGE_KEY)[keyof typeof STORAGE_KEY];

export const appStorage = new MMKV({ id: 'deckly' });

export const zustandStorage: StateStorage = {
  getItem: (name) => appStorage.getString(name) ?? null,
  setItem: (name, value) => {
    appStorage.set(name, value);
  },
  removeItem: (name) => {
    appStorage.delete(name);
  },
};
