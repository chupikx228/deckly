import type { Locale } from '@/shared/config';

const DAY_MS = 24 * 60 * 60 * 1000;

export const formatDate = (timestamp: number, locale: Locale): string =>
  new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'short', year: 'numeric' }).format(
    new Date(timestamp),
  );

export const formatRelativeDate = (timestamp: number, locale: Locale, now: number): string => {
  const days = Math.round((timestamp - now) / DAY_MS);

  if (Math.abs(days) > 30) {
    return formatDate(timestamp, locale);
  }

  return new Intl.RelativeTimeFormat(locale, { numeric: 'auto' }).format(days, 'day');
};

export const formatPercent = (ratio: number, locale: Locale): string =>
  new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 0 }).format(ratio);

export const formatCount = (value: number, locale: Locale): string =>
  new Intl.NumberFormat(locale).format(value);
