import { AbstractControl, FormArray, FormControl, FormGroup, ValidatorFn } from '@angular/forms';
import { Json } from './source-api';

export const PODCAST_MAX_FEEDS = 32;
export const PODCAST_MAX_URL_LENGTH = 8192;
export type ScheduleMode = 'RANDOM' | 'SMART';
type Num = number | string | null;

export const scheduleDescriptions: Record<ScheduleMode, string> = {
  RANDOM: 'Shuffle-bag style rotation through eligible episodes.',
  SMART: 'Prefers unplayed episodes, then uses playback history, freshness and feed diversity.',
};

function blank(value: unknown): boolean {
  return value === null || value === undefined || String(value).trim() === '';
}

function integerAtLeast(value: unknown, min: number): boolean {
  const n = Number(value);
  return Number.isSafeInteger(n) && n >= min;
}

// Mirrors the backend: HTTP(S), a host, no credentials, no whitespace/control characters.
export function validHttpUrl(value: string): boolean {
  if (value.length > PODCAST_MAX_URL_LENGTH || !/^https?:\/\//i.test(value)) return false;
  if (/\s/.test(value) || [...value].some((c) => c.charCodeAt(0) < 32 || c.charCodeAt(0) === 127))
    return false;
  try {
    const url = new URL(value);
    return !!url.hostname && !url.username && !url.password;
  } catch {
    return false;
  }
}

const feedUrl: ValidatorFn = (control) => {
  const value = String(control.value ?? '').trim();
  if (!value) return { required: true };
  return validHttpUrl(value) ? null : { url: true };
};
const optionalLimit: ValidatorFn = (control) =>
  blank(control.value) || integerAtLeast(control.value, 0) ? null : { integer: true };
const refreshInterval: ValidatorFn = (control) =>
  blank(control.value)
    ? { required: true }
    : integerAtLeast(control.value, 1)
      ? null
      : { integer: true };
const cooldown: ValidatorFn = (control) =>
  blank(control.value)
    ? { required: true }
    : integerAtLeast(control.value, 0)
      ? null
      : { integer: true };
const uniqueFeeds: ValidatorFn = (control) => {
  const urls = (control as FormArray).controls
    .map((feed) => String(feed.get('url')?.value ?? '').trim())
    .filter(Boolean);
  return new Set(urls).size === urls.length ? null : { duplicate: true };
};
const feedCount: ValidatorFn = (control) => {
  const count = (control as FormArray).length;
  return count < 1 || count > PODCAST_MAX_FEEDS ? { count: true } : null;
};

export function newFeedGroup(url = '', maxAge: unknown = null, maxEpisodes: unknown = null) {
  return new FormGroup({
    url: new FormControl<string>(url, { nonNullable: true, validators: [feedUrl] }),
    max_age_days: new FormControl<Num>(blank(maxAge) ? null : (maxAge as Num), {
      validators: [optionalLimit],
    }),
    max_episodes: new FormControl<Num>(blank(maxEpisodes) ? null : (maxEpisodes as Num), {
      validators: [optionalLimit],
    }),
  });
}

export type FeedGroup = ReturnType<typeof newFeedGroup>;

// Defaults are the backend defaults: SMART, 30 minutes and 24 hours.
export function buildPodcastForm(values: Record<string, unknown> = {}) {
  const saved = Array.isArray(values['feeds'])
    ? (values['feeds'] as Record<string, unknown>[])
    : [];
  const feeds = saved.length
    ? saved.map((f) => newFeedGroup(String(f['url'] ?? ''), f['max_age_days'], f['max_episodes']))
    : [newFeedGroup()];
  return new FormGroup({
    feeds: new FormArray(feeds, [uniqueFeeds, feedCount]),
    schedule_mode: new FormControl<ScheduleMode>(
      values['schedule_mode'] === 'RANDOM' ? 'RANDOM' : 'SMART',
      { nonNullable: true },
    ),
    refresh_interval_minutes: new FormControl<Num>(
      blank(values['refresh_interval_minutes']) ? 30 : (values['refresh_interval_minutes'] as Num),
      { validators: [refreshInterval] },
    ),
    replay_cooldown_hours: new FormControl<Num>(
      blank(values['replay_cooldown_hours']) ? 24 : (values['replay_cooldown_hours'] as Num),
      { validators: [cooldown] },
    ),
  });
}

export type PodcastForm = ReturnType<typeof buildPodcastForm>;

function limit(value: unknown): number | null {
  return blank(value) ? null : Number(value);
}

// Caller has already checked form validity. Blank limits are null, never a magic number.
export function podcastConfig(form: PodcastForm): Record<string, Json> {
  const v = form.getRawValue();
  return {
    feeds: v.feeds.map((f) => ({
      url: f.url.trim(),
      max_age_days: limit(f.max_age_days),
      max_episodes: limit(f.max_episodes),
    })),
    schedule_mode: v.schedule_mode,
    refresh_interval_minutes: Number(v.refresh_interval_minutes),
    replay_cooldown_hours: Number(v.replay_cooldown_hours),
  };
}

const messages: Record<string, Record<string, string>> = {
  url: {
    required: 'Enter the feed URL.',
    url: 'Enter an HTTP or HTTPS feed URL without credentials or spaces.',
  },
  limit: { integer: 'Enter a whole number of 0 or more, or leave blank for no limit.' },
  refresh: {
    required: 'Enter the refresh interval in minutes.',
    integer: 'Enter a whole number of minutes, 1 or more.',
  },
  cooldown: {
    required: 'Enter the replay cooldown in hours.',
    integer: 'Enter a whole number of hours, 0 or more.',
  },
};

export function controlError(control: AbstractControl | null, kind: keyof typeof messages): string {
  if (!control?.errors || !(control.touched || control.dirty)) return '';
  const key = Object.keys(control.errors)[0];
  return messages[kind][key] ?? '';
}

export function feedsError(array: FormArray): string {
  if (!(array.touched || array.dirty) || !array.errors) return '';
  if (array.errors['duplicate']) return 'Feed URLs must be unique within a source.';
  return `Add between 1 and ${PODCAST_MAX_FEEDS} feeds.`;
}

const feedErrors: Record<string, string> = {
  PODCAST_FETCH_TIMEOUT: 'Timed out',
  PODCAST_FETCH_FAILED: 'Fetch failed',
  PODCAST_FEED_INVALID: 'Not a usable RSS/Atom feed',
  PODCAST_FEED_TOO_LARGE: 'Feed too large',
  PODCAST_ENCODING_ERROR: 'Unsupported encoding',
};

export function feedErrorText(code: string): string {
  return feedErrors[code] ?? code;
}
