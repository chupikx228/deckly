import { appStorage, createId } from '@/shared/lib';

const CLIENT_ID_KEY = 'client-id';

let cached: string | null = null;

export const getClientId = (): string => {
  if (cached !== null) {
    return cached;
  }

  const stored = appStorage.getString(CLIENT_ID_KEY);

  if (stored !== undefined) {
    cached = stored;

    return stored;
  }

  const created = createId();

  appStorage.set(CLIENT_ID_KEY, created);
  cached = created;

  return created;
};
