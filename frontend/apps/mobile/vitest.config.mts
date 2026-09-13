import { fileURLToPath } from 'node:url';

import { defineConfig } from 'vitest/config';

const resolveSrc = (segment: string): string =>
  fileURLToPath(new URL(`./src/${segment}`, import.meta.url));

export default defineConfig({
  resolve: {
    alias: {
      '@/shared': resolveSrc('shared'),
      '@/entities': resolveSrc('entities'),
      '@/features': resolveSrc('features'),
    },
  },
  test: {
    include: ['src/**/*.test.ts'],
  },
});
