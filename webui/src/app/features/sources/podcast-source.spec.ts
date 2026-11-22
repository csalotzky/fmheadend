import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideRouter, ActivatedRoute, convertToParamMap, Router } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { SourceEditor } from './source-editor';
import { PodcastFeedProbe } from './source-api';
import { DashboardState } from '../dashboard/dashboard-state';
import { sample } from '../dashboard/test-fixtures';

type Feed = { url: string; max_age_days: number | null; max_episodes: number | null };
const saved = (feeds: Feed[], extra: Record<string, unknown> = {}) => ({
  feeds,
  schedule_mode: 'SMART',
  refresh_interval_minutes: 30,
  replay_cooldown_hours: 24,
  ...extra,
});
const bareFeed = (url: string): Feed => ({ url, max_age_days: null, max_episodes: null });
const testUrl = '/api/v1/sources/test';
function feedResponse(feed: PodcastFeedProbe) {
  return {
    ok: (feed.eligible_episodes ?? 0) > 0,
    mocked: false,
    message: 'Podcast feed diagnostics',
    podcast: {
      feeds_attempted: 1,
      feeds_succeeded: feed.error ? 0 : 1,
      discovered_episodes: feed.discovered_episodes,
      eligible_episodes: feed.eligible_episodes ?? 0,
      feeds: [feed],
    },
  };
}

describe('Podcast source configuration', () => {
  let http: HttpTestingController;
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({}) } } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    vi.spyOn(TestBed.inject(DashboardState), 'refreshAfterMutation').mockResolvedValue();
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  });
  afterEach(() => http.verify());

  async function editor(config?: Record<string, unknown>): Promise<ComponentFixture<SourceEditor>> {
    if (config)
      Object.assign(TestBed.inject(ActivatedRoute).snapshot, {
        paramMap: convertToParamMap({ id: '1' }),
      });
    const f = TestBed.createComponent(SourceEditor);
    f.detectChanges();
    http.expectOne('/api/v1/audio-profiles/files').flush([]);
    if (config)
      http
        .expectOne('/api/v1/sources/1')
        .flush({ ...sample().sources[0], type: 'PODCAST', config });
    else {
      const select: HTMLSelectElement = f.nativeElement.querySelector('[formControlName="type"]');
      select.value = 'PODCAST';
      select.dispatchEvent(new Event('change'));
      f.componentInstance.form.controls.name.setValue('Talk');
    }
    await f.whenStable();
    f.detectChanges();
    return f;
  }
  const el = (f: ComponentFixture<SourceEditor>) => f.nativeElement as HTMLElement;
  const input = (f: ComponentFixture<SourceEditor>, label: string) =>
    el(f).querySelector(`[aria-label="${label}"]`) as HTMLInputElement;
  function type(f: ComponentFixture<SourceEditor>, node: HTMLInputElement, value: string) {
    node.value = value;
    node.dispatchEvent(new Event('input'));
    f.detectChanges();
  }
  const button = (f: ComponentFixture<SourceEditor>, text: string) =>
    [...el(f).querySelectorAll('button')].find((b) =>
      b.textContent?.trim().includes(text),
    ) as HTMLButtonElement;
  async function save(f: ComponentFixture<SourceEditor>, method: string, url: string) {
    const done = f.componentInstance.save();
    await Promise.resolve();
    const req = http.expectOne(url);
    expect(req.request.method).toBe(method);
    req.flush({ ...sample().sources[0], type: 'PODCAST' });
    await done;
    return req.request.body;
  }

  it('offers PODCAST and renders its configuration', async () => {
    const f = await editor();
    const select: HTMLSelectElement = el(f).querySelector('[formControlName="type"]')!;
    expect(select.querySelector('option[value="PODCAST"]')).toBeTruthy();
    expect(el(f).querySelectorAll('.feed-card')).toHaveLength(1);
    expect(button(f, '+ Add feed')).toBeTruthy();
    expect(f.componentInstance.podcast.getRawValue().schedule_mode).toBe('SMART');
    expect(f.componentInstance.podcast.getRawValue().refresh_interval_minutes).toBe(30);
    expect(f.componentInstance.podcast.getRawValue().replay_cooldown_hours).toBe(24);
  });

  it('loads an existing source preserving order, limits and settings', async () => {
    const feeds = [
      { url: 'https://b.example/f.xml', max_age_days: null, max_episodes: null },
      { url: 'https://a.example/f.xml', max_age_days: 14, max_episodes: 5 },
    ];
    const f = await editor(saved(feeds, { schedule_mode: 'RANDOM', refresh_interval_minutes: 90 }));
    expect(input(f, 'Feed 1 URL').value).toBe('https://b.example/f.xml');
    expect(input(f, 'Feed 2 URL').value).toBe('https://a.example/f.xml');
    expect(input(f, 'Feed 1 maximum age in days').value).toBe('');
    expect(input(f, 'Feed 2 maximum age in days').value).toBe('14');
    expect(input(f, 'Feed 2 maximum episodes').value).toBe('5');
    expect(f.componentInstance.scheduleMode()).toBe('RANDOM');
  });

  it('round-trips an unchanged config exactly, preserving nulls and order', async () => {
    const config = saved([
      { url: 'https://b.example/f.xml', max_age_days: null, max_episodes: null },
      { url: 'https://a.example/f.xml', max_age_days: 0, max_episodes: 7 },
    ]);
    const f = await editor(config);
    const body = await save(f, 'PATCH', '/api/v1/sources/1');
    expect(body.type).toBe('PODCAST');
    expect(body.config).toEqual(config);
  });

  it('edits URLs, adds and removes feeds in order', async () => {
    const f = await editor();
    type(f, input(f, 'Feed 1 URL'), ' https://one.example/rss ');
    button(f, '+ Add feed').click();
    f.detectChanges();
    type(f, input(f, 'Feed 2 URL'), 'https://two.example/rss');
    button(f, '+ Add feed').click();
    f.detectChanges();
    type(f, input(f, 'Feed 3 URL'), 'http://three.example/rss');
    expect(el(f).querySelectorAll('.feed-card')).toHaveLength(3);
    button(f, 'Remove').parentElement!.querySelector<HTMLButtonElement>('.feed-remove')!.click();
    f.detectChanges();
    expect(input(f, 'Feed 1 URL').value).toBe('https://two.example/rss');
    expect(input(f, 'Feed 2 URL').value).toBe('http://three.example/rss');
    const body = await save(f, 'POST', '/api/v1/sources');
    expect(body.config.feeds.map((x: Feed) => x.url)).toEqual([
      'https://two.example/rss',
      'http://three.example/rss',
    ]);
  });

  it('does not remove the last feed', async () => {
    const f = await editor();
    expect((el(f).querySelector('.feed-remove') as HTMLButtonElement).disabled).toBe(true);
  });

  it('submits blank limits as null, numeric limits as numbers, and both together', async () => {
    const f = await editor();
    type(f, input(f, 'Feed 1 URL'), 'https://one.example/rss');
    button(f, '+ Add feed').click();
    f.detectChanges();
    type(f, input(f, 'Feed 2 URL'), 'https://two.example/rss');
    type(f, input(f, 'Feed 2 maximum age in days'), '30');
    type(f, input(f, 'Feed 2 maximum episodes'), '10');
    button(f, '+ Add feed').click();
    f.detectChanges();
    type(f, input(f, 'Feed 3 URL'), 'https://three.example/rss');
    type(f, input(f, 'Feed 3 maximum age in days'), '0');
    const body = await save(f, 'POST', '/api/v1/sources');
    expect(body.config.feeds).toEqual([
      { url: 'https://one.example/rss', max_age_days: null, max_episodes: null },
      { url: 'https://two.example/rss', max_age_days: 30, max_episodes: 10 },
      { url: 'https://three.example/rss', max_age_days: 0, max_episodes: null },
    ]);
  });

  it('keeps cooldown when switching RANDOM and SMART and saves exact shape', async () => {
    const f = await editor();
    type(f, input(f, 'Feed 1 URL'), 'https://one.example/rss');
    const mode: HTMLSelectElement = el(f).querySelector('[formControlName="schedule_mode"]')!;
    const cooldown = el(f).querySelector(
      '[formControlName="replay_cooldown_hours"]',
    ) as HTMLInputElement;
    const refresh = el(f).querySelector(
      '[formControlName="refresh_interval_minutes"]',
    ) as HTMLInputElement;
    type(f, cooldown, '48');
    type(f, refresh, '15');
    mode.value = 'RANDOM';
    mode.dispatchEvent(new Event('change'));
    f.detectChanges();
    expect(cooldown.value).toBe('48');
    expect(el(f).textContent).toContain('Shuffle-bag');
    mode.value = 'SMART';
    mode.dispatchEvent(new Event('change'));
    f.detectChanges();
    expect(cooldown.value).toBe('48');
    expect(el(f).textContent).toContain('Prefers unplayed episodes');
    mode.value = 'RANDOM';
    mode.dispatchEvent(new Event('change'));
    const body = await save(f, 'POST', '/api/v1/sources');
    expect(body.config).toEqual({
      feeds: [{ url: 'https://one.example/rss', max_age_days: null, max_episodes: null }],
      schedule_mode: 'RANDOM',
      refresh_interval_minutes: 15,
      replay_cooldown_hours: 48,
    });
  });

  it('validates the config but not feed reachability', async () => {
    const f = await editor();
    const c = f.componentInstance;
    type(f, input(f, 'Feed 1 URL'), 'ftp://x.example/feed');
    type(f, input(f, 'Feed 1 maximum age in days'), '-1');
    const refresh = el(f).querySelector(
      '[formControlName="refresh_interval_minutes"]',
    ) as HTMLInputElement;
    type(f, refresh, '0');
    await c.save();
    f.detectChanges();
    http.expectNone('/api/v1/sources');
    const text = el(f).textContent!;
    expect(text).toContain('HTTP or HTTPS feed URL');
    expect(text).toContain('0 or more');
    expect(text).toContain('minutes, 1 or more');
    expect(input(f, 'Feed 1 URL').getAttribute('aria-invalid')).toBe('true');
    expect(input(f, 'Feed 1 URL').getAttribute('aria-describedby')).toBe('feed-0-url-error');
    type(f, input(f, 'Feed 1 URL'), 'https://user:pw@x.example/f');
    expect(c.podcast.controls.feeds.at(0).controls.url.valid).toBe(false);
    type(f, input(f, 'Feed 1 URL'), 'https://offline.invalid/feed.xml');
    type(f, input(f, 'Feed 1 maximum age in days'), '');
    type(f, refresh, '5');
    const body = await save(f, 'POST', '/api/v1/sources');
    expect(body.config.feeds[0].url).toBe('https://offline.invalid/feed.xml');
  });

  it('rejects duplicate feed URLs', async () => {
    const f = await editor();
    type(f, input(f, 'Feed 1 URL'), 'https://one.example/rss');
    button(f, '+ Add feed').click();
    f.detectChanges();
    type(f, input(f, 'Feed 2 URL'), 'https://one.example/rss');
    await f.componentInstance.save();
    f.detectChanges();
    http.expectNone('/api/v1/sources');
    expect(el(f).textContent).toContain('must be unique');
  });

  it('tests current unsaved values without saving and shows counts', async () => {
    const f = await editor();
    type(f, input(f, 'Feed 1 URL'), 'https://one.example/rss');
    type(f, input(f, 'Feed 1 maximum episodes'), '3');
    expect(button(f, 'Test feeds')).toBeTruthy();
    const done = f.componentInstance.test();
    await Promise.resolve();
    const req = http.expectOne('/api/v1/sources/test');
    expect(req.request.body.type).toBe('PODCAST');
    expect(req.request.body.config.feeds).toEqual([
      { url: 'https://one.example/rss', max_age_days: null, max_episodes: 3 },
    ]);
    req.flush({
      ok: true,
      mocked: false,
      message: 'Podcast catalog available; audio playback was not tested',
      podcast: {
        feeds_attempted: 1,
        feeds_succeeded: 1,
        discovered_episodes: 47,
        eligible_episodes: 10,
        feeds: [
          {
            feed_id: 'abc',
            title: 'Telex Podcast',
            error: null,
            discovered_episodes: 47,
            eligible_episodes: 10,
          },
        ],
      },
    });
    await done;
    f.detectChanges();
    const text = el(f).querySelector('app-source-test-result')!.textContent!;
    expect(text).toContain('Feed test successful');
    expect(text).not.toContain('Podcast catalog available');
    expect(text).not.toContain('audio playback');
    expect(el(f).textContent).not.toContain('reachable');
    expect(el(f).querySelector('#podcast-feeds-label')!.textContent!.trim()).toBe('Feeds');
    expect(el(f).textContent).not.toMatch(/[\u2013\u2014]/);
    expect(text).toContain('1 / 1');
    expect(text).toContain('47 discovered');
    expect(text).toContain('10');
    expect(text).not.toContain('https://one.example/rss');
    const card = el(f).querySelector('.feed-card')!;
    expect(card.querySelector('.feed-result')!.textContent).toContain(
      'OK: 47 discovered / 10 eligible',
    );
    expect(card.querySelector('.feed-result')!.textContent).toContain('Telex Podcast');
    expect(card.querySelector('.feed-result')!.textContent).not.toContain('https://');
    http.expectNone('/api/v1/sources');
  });

  it('tests feeds sequentially and shows progress and results before the batch finishes', async () => {
    const feeds = [
      { url: 'https://good.example/rss', max_age_days: 14, max_episodes: 12 },
      { ...bareFeed('https://bad.example/rss'), max_episodes: 0 },
      { ...bareFeed('https://empty.example/rss'), max_age_days: 0 },
    ];
    const config = saved(feeds, { schedule_mode: 'RANDOM', refresh_interval_minutes: 90 });
    const f = await editor(config);
    const done = f.componentInstance.test();
    const diagnostics = [
      { feed_id: 'a', title: 'Good Show', error: null, discovered_episodes: 180, eligible_episodes: 12 },
      { feed_id: 'b', error: 'PODCAST_FETCH_TIMEOUT', discovered_episodes: 0, eligible_episodes: null },
      { feed_id: 'c', error: null, discovered_episodes: 10, eligible_episodes: 0 },
    ];
    const progress = () => el(f).querySelector('.feed-test-progress')!;
    const cards = [...el(f).querySelectorAll('.feed-card')];
    for (const [index, feed] of feeds.entries()) {
      f.detectChanges();
      const req = http.expectOne(testUrl);
      expect(req.request.method).toBe('POST');
      expect(req.request.body.type).toBe('PODCAST');
      expect(req.request.body.name).toBe(sample().sources[0].name);
      expect(req.request.body.config).toEqual({ ...config, feeds: [feed] });
      expect(progress().querySelector('progress')!.value).toBe(index);
      expect(progress().textContent).toContain(`${index} / 3 feeds tested`);
      expect(progress().textContent).toContain(feed.url);
      expect(el(f).querySelectorAll('.feed-result')).toHaveLength(index);
      expect(el(f).querySelector('app-source-test-result')!.textContent!.trim()).toBe('');
      await f.componentInstance.test(); // The active run owns the only request.
      http.expectNone(testUrl);
      req.flush(feedResponse(diagnostics[index]));
      await Promise.resolve();
    }
    await done;
    f.detectChanges();
    const result = el(f).querySelector('app-source-test-result')!;
    expect(result.querySelector('.warning')).toBeTruthy();
    expect(result.querySelector('.error')).toBeNull();
    expect(result.textContent).toContain('1 of 3 feeds failed');
    expect(result.textContent).not.toContain('OK: 180');
    expect(result.textContent).not.toContain('Timed out');
    expect(cards[0].querySelector('.feed-result')!.textContent).toContain(
      'OK: 180 discovered / 12 eligible',
    );
    expect(cards[0].querySelector('.feed-result .error')).toBeNull();
    expect(cards[1].querySelector('.feed-result')!.textContent).toContain('Feed title unavailable');
    expect(cards[1].querySelector('.feed-result')!.textContent).not.toContain('bad.example');
    expect(cards[0].querySelector('.feed-result')!.textContent).toContain('Good Show');
    expect(cards[1].querySelector('.feed-result')!.textContent).not.toContain('eligible');
    expect(cards[1].querySelector('.feed-result .error')!.textContent).toContain(
      'Error: Timed out',
    );
    expect(cards[2].querySelector('.feed-result')!.textContent).toContain('OK: 10 discovered / 0 eligible');
    expect(result.textContent).toContain('2 / 3');
    expect(result.textContent).toContain('190 discovered');
    expect([...result.querySelectorAll('dd')].at(-1)?.textContent).toBe('12');
    expect(progress().textContent).toContain('3 / 3 feeds tested');
    expect(progress().textContent).not.toContain('Testing feed');
    http.expectNone('/api/v1/sources/1');
  });

  it('reports total feed failure as an error', async () => {
    const f = await editor();
    type(f, input(f, 'Feed 1 URL'), 'https://bad.example/rss');
    const done = f.componentInstance.test();
    await Promise.resolve();
    http.expectOne('/api/v1/sources/test').flush({
      ok: false,
      mocked: false,
      message: 'NO_PLAYABLE_EPISODES: no eligible podcast enclosures',
      podcast: {
        feeds_attempted: 1,
        feeds_succeeded: 0,
        discovered_episodes: 0,
        eligible_episodes: 0,
        feeds: [{ feed_id: 'b', error: 'PODCAST_FEED_INVALID', discovered_episodes: 0 }],
      },
    });
    await done;
    f.detectChanges();
    const result = el(f).querySelector('app-source-test-result')!;
    expect(result.querySelector('.error')).toBeTruthy();
    expect(result.textContent).toContain('Feed test failed');
    expect(el(f).querySelector('.feed-card .feed-result')!.textContent).toContain(
      'Error: Not a usable RSS/Atom feed',
    );
  });

  it('reports a successful feed with zero eligible episodes separately from RSS failures', async () => {
    const f = await editor(saved([bareFeed('https://zero.example/rss')]));
    const done = f.componentInstance.test();
    http.expectOne(testUrl).flush(feedResponse({
      feed_id: 'zero', error: null, discovered_episodes: 7, eligible_episodes: 0,
    }));
    await done;
    f.detectChanges();
    const summary = el(f).querySelector('app-source-test-result')!;
    expect(summary.textContent).toContain('Feed test found no eligible episodes');
    expect(summary.textContent).toContain('1 / 1');
    expect(el(f).querySelector('.feed-result')!.textContent).toContain('OK: 7 discovered / 0 eligible');
  });

  it.each([
    [503, 'DISCOVERY_BUSY', 'Discovery is busy or shutting down'],
    [503, 'DISCOVERY_TIMEOUT', 'Discovery did not finish within its timeout'],
    [502, 'UPSTREAM_ERROR', 'The server could not complete the request'],
  ])('stops with partial results on HTTP %i %s', async (status, code, message) => {
    const f = await editor(saved([
      bareFeed('https://good.example/rss'),
      bareFeed('https://interrupted.example/rss'),
      bareFeed('https://not-started.example/rss'),
    ]));
    const done = f.componentInstance.test();
    http.expectOne(testUrl).flush(feedResponse({
      feed_id: 'good', title: 'Good Show', error: null, discovered_episodes: 5, eligible_episodes: 3,
    }));
    await Promise.resolve();
    http.expectOne(testUrl).flush({ error: code, message }, { status, statusText: 'Error' });
    await done;
    f.detectChanges();
    http.expectNone(testUrl);
    expect(el(f).querySelector('.feed-test-progress')!.textContent).toContain('1 / 3 feeds tested');
    expect(el(f).querySelector('.feed-test-progress')!.textContent).toContain('Test stopped');
    expect(el(f).querySelector('app-source-test-result [role="alert"]')!.textContent).toContain(
      `${code}: ${message}`,
    );
    expect(el(f).querySelectorAll('.feed-result')).toHaveLength(1);
    expect(el(f).querySelector('.feed-result')!.textContent).toContain('Good Show');
    expect(button(f, 'Test feeds').disabled).toBe(false);
  });

  it('stops after the client timeout and resets progress when testing again', async () => {
    const f = await editor(saved([
      bareFeed('https://a.example/rss'), bareFeed('https://b.example/rss'),
    ]));
    vi.useFakeTimers();
    try {
      const done = f.componentInstance.test();
      http.expectOne(testUrl).flush(feedResponse({
        feed_id: 'a', error: null, discovered_episodes: 1, eligible_episodes: 1,
      }));
      await Promise.resolve();
      const pending = http.expectOne(testUrl);
      await vi.advanceTimersByTimeAsync(195_000);
      expect(pending.cancelled).toBe(false);
      await vi.advanceTimersByTimeAsync(10_000);
      await done;
      f.detectChanges();
      expect(pending.cancelled).toBe(true);
      http.expectNone(testUrl);
      expect(el(f).querySelector('.feed-test-progress')!.textContent).toContain('1 / 2 feeds tested');
      expect(f.componentInstance.testError()).toContain('timed out after 205 seconds');
      const retry = f.componentInstance.test();
      f.detectChanges();
      expect(el(f).querySelector('.feed-test-progress')!.textContent).toContain('0 / 2 feeds tested');
      expect(el(f).querySelectorAll('.feed-result')).toHaveLength(0);
      http.expectOne(testUrl).flush({ error: 'DISCOVERY_BUSY', message: 'Discovery is busy' }, {
        status: 503, statusText: 'Service Unavailable',
      });
      await retry;
    } finally {
      vi.useRealTimers();
    }
  });

  it('discards an in-flight result and stops the old batch after editing the configuration', async () => {
    const f = await editor(saved([
      bareFeed('https://a.example/rss'),
      bareFeed('https://b.example/rss'),
      bareFeed('https://c.example/rss'),
    ]));
    const c = f.componentInstance;
    const done = c.test();
    http.expectOne(testUrl).flush(feedResponse({
      feed_id: 'a', error: null, discovered_episodes: 1, eligible_episodes: 1,
    }));
    await Promise.resolve();
    const pending = http.expectOne(testUrl);
    c.feeds.at(1).controls.url.setValue('https://changed.example/rss');
    expect(c.podcastTestProgress()).toBeNull();
    await c.test();
    http.expectNone(testUrl);
    pending.flush(feedResponse({
      feed_id: 'b', title: 'Stale title', error: null, discovered_episodes: 2, eligible_episodes: 2,
    }));
    await done;
    f.detectChanges();
    http.expectNone(testUrl);
    expect(c.testing()).toBe(false);
    expect(c.result()).toBeNull();
    expect(el(f).querySelectorAll('.feed-result')).toHaveLength(0);
  });

  it('allows leaving during a test and cancels the client subscription on destruction', async () => {
    const f = await editor(saved([
      bareFeed('https://a.example/rss'), bareFeed('https://b.example/rss'),
    ]));
    const done = f.componentInstance.test();
    const pending = http.expectOne(testUrl);
    f.detectChanges();
    expect(button(f, 'Cancel').disabled).toBe(false);
    f.destroy();
    await done;
    expect(pending.cancelled).toBe(true);
    http.expectNone(testUrl);
  });

  it('keeps the shared Audio processing panel and no playback controls', async () => {
    const f = await editor();
    const panel = el(f).querySelector('[aria-labelledby="audio-processing-heading"]')!;
    expect(panel.textContent).toContain('Audio Profile');
    expect(panel.textContent).not.toContain('Processor');
    expect(panel.textContent).toContain('Source Gain');
    expect(panel.textContent).toContain('Advanced source options');
    expect(el(f).querySelector('[aria-labelledby="source-heading"]')!.contains(panel)).toBe(false);
    for (const word of ['Play', 'Next', 'Previous', 'Skip'])
      expect(
        [...el(f).querySelectorAll('button')].some((b) => b.textContent?.trim() === word),
      ).toBe(false);
  });

  it('humanizes type labels while keeping backend enum values', async () => {
    const f = await editor();
    const select: HTMLSelectElement = el(f).querySelector('[formControlName="type"]')!;
    const labels = Object.fromEntries(
      [...select.options].map((o) => [o.value, o.textContent?.trim()]),
    );
    expect(labels).toEqual({
      HTTP: 'Internet stream (HTTP)',
      UDP: 'Network stream (UDP)',
      RTP: 'Network stream (RTP)',
      ALSA: 'Sound card (ALSA)',
      LOCAL: 'Local media',
      PODCAST: 'Podcast',
    });
    expect(f.componentInstance.form.controls.type.value).toBe('PODCAST');
    select.value = 'ALSA';
    select.dispatchEvent(new Event('change'));
    f.detectChanges();
    expect(f.componentInstance.form.controls.type.value).toBe('ALSA');
    expect(select.selectedOptions[0].textContent?.trim()).toBe('Sound card (ALSA)');
    http.expectOne('/api/v1/alsa/capture-devices').flush({ devices: [] });
  });

  it('places the single Test feeds button beside Add feed with results below it', async () => {
    const f = await editor();
    type(f, input(f, 'Feed 1 URL'), 'https://one.example/rss');
    const tests = [...el(f).querySelectorAll('button')].filter((b) =>
      b.textContent?.includes('Test feeds'),
    );
    expect(tests).toHaveLength(1);
    const actions = el(f).querySelector('.feed-actions')!;
    expect(actions.firstElementChild).toBe(button(f, '+ Add feed'));
    expect(actions.lastElementChild).toBe(tests[0]);
    expect(el(f).querySelector('.editor-actions')!.contains(tests[0])).toBe(false);
    expect(el(f).querySelector('[aria-labelledby="source-heading"]')!.contains(tests[0])).toBe(
      true,
    );
    const done = f.componentInstance.test();
    await Promise.resolve();
    http.expectOne('/api/v1/sources/test').flush({
      ok: true,
      mocked: false,
      message: 'Podcast catalog available; audio playback was not tested',
      podcast: {
        feeds_attempted: 1,
        feeds_succeeded: 1,
        discovered_episodes: 2,
        eligible_episodes: 2,
        feeds: [{ feed_id: 'a', error: null, discovered_episodes: 2, eligible_episodes: 2 }],
      },
    });
    await done;
    f.detectChanges();
    const results = el(f).querySelectorAll('app-source-test-result');
    expect(results).toHaveLength(1);
    expect(actions.nextElementSibling?.classList.contains('feed-test-progress')).toBe(true);
    expect(actions.nextElementSibling?.nextElementSibling).toBe(results[0]);
    const heading = results[0].nextElementSibling!;
    expect(heading.tagName).toBe('H3');
    expect(heading.textContent).toBe('Playback scheduling');
    expect(heading.nextElementSibling?.classList.contains('podcast-settings')).toBe(true);
  });

  it('keeps saved source types unchanged on round trip', async () => {
    const f = await editor(
      saved([{ url: 'https://a.example/f.xml', max_age_days: null, max_episodes: null }]),
    );
    expect(f.componentInstance.form.controls.type.value).toBe('PODCAST');
    const body = await save(f, 'PATCH', '/api/v1/sources/1');
    expect(body.type).toBe('PODCAST');
  });

  it('shows no per-feed rows before testing and hides them when the feed URL changes', async () => {
    const f = await editor();
    type(f, input(f, 'Feed 1 URL'), 'https://one.example/rss');
    button(f, '+ Add feed').click();
    f.detectChanges();
    type(f, input(f, 'Feed 2 URL'), 'https://two.example/rss');
    expect(el(f).querySelectorAll('.feed-result')).toHaveLength(0);
    const done = f.componentInstance.test();
    await Promise.resolve();
    http.expectOne(testUrl).flush(feedResponse({
      feed_id: 'a', title: 'Show A', error: null, discovered_episodes: 5, eligible_episodes: 0,
    }));
    await Promise.resolve();
    http.expectOne(testUrl).flush(feedResponse({
      feed_id: 'b', title: null, error: null, discovered_episodes: 3, eligible_episodes: 3,
    }));
    await done;
    f.detectChanges();
    const cards = el(f).querySelectorAll('.feed-card');
    expect(cards[0].querySelector('.feed-result')!.textContent).toContain(
      'OK: 5 discovered / 0 eligible',
    );
    expect(cards[0].querySelector('.feed-result')!.textContent).toContain('Show A');
    expect(cards[1].querySelector('.feed-result')!.textContent).toContain(
      'OK: 3 discovered / 3 eligible',
    );
    expect(cards[1].querySelector('.feed-result')!.textContent).toContain('Feed title unavailable');
    expect(cards[0].querySelector('.feed-url')!.nextElementSibling?.className).toContain(
      'feed-result',
    );
    const summary = el(f).querySelector('app-source-test-result')!.textContent!;
    expect(summary).toContain('2 / 2');
    expect(summary).toContain('8 discovered');
    type(f, input(f, 'Feed 1 URL'), 'https://changed.example/rss');
    expect(el(f).querySelectorAll('.feed-result')).toHaveLength(0);
    expect(el(f).querySelector('app-source-test-result')!.textContent!.trim()).toBe('');
    (el(f).querySelectorAll('.feed-remove')[0] as HTMLButtonElement).click();
    f.detectChanges();
    expect(el(f).querySelector('.feed-result')).toBeNull();
  });
});
