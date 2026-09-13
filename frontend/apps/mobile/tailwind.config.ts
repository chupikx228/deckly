import type { Config } from 'tailwindcss';
import nativewindPreset from 'nativewind/preset';

const withAlpha = (variable: string) => `rgb(var(${variable}) / <alpha-value>)`;

export default {
  content: ['./app/**/*.{ts,tsx}', './src/**/*.{ts,tsx}'],
  presets: [nativewindPreset],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        background: withAlpha('--color-background'),
        surface: withAlpha('--color-surface'),
        'surface-raised': withAlpha('--color-surface-raised'),
        border: withAlpha('--color-border'),
        foreground: withAlpha('--color-foreground'),
        muted: withAlpha('--color-muted'),
        primary: {
          DEFAULT: withAlpha('--color-primary'),
          soft: withAlpha('--color-primary-soft'),
          foreground: withAlpha('--color-primary-foreground'),
        },
        accent: withAlpha('--color-accent'),
        success: withAlpha('--color-success'),
        warning: withAlpha('--color-warning'),
        danger: withAlpha('--color-danger'),
        rating: {
          again: withAlpha('--color-rating-again'),
          hard: withAlpha('--color-rating-hard'),
          good: withAlpha('--color-rating-good'),
          easy: withAlpha('--color-rating-easy'),
        },
      },
      borderRadius: {
        card: '20px',
        sheet: '32px',
      },
      fontSize: {
        display: ['34px', { lineHeight: '38px', letterSpacing: '-0.8px' }],
        title: ['26px', { lineHeight: '32px', letterSpacing: '-0.4px' }],
        heading: ['20px', { lineHeight: '26px', letterSpacing: '-0.2px' }],
        body: ['16px', { lineHeight: '24px' }],
        label: ['14px', { lineHeight: '20px' }],
        caption: ['12px', { lineHeight: '16px' }],
      },
      spacing: {
        gutter: '20px',
        section: '32px',
      },
    },
  },
  plugins: [],
} satisfies Config;
