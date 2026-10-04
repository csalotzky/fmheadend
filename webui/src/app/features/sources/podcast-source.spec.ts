import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideRouter, ActivatedRoute, convertToParamMap, Router } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { SourceEditor } from './source-editor';
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
        feeds: [{ feed_id: 'abc', error: null, discovered_episodes: 47 }],
      },
    });
    await done;
    f.detectChanges();
    const text = el(f).querySelector('app-source-test-result')!.textContent!;
    expect(text).toContain('Feed test successful');
    expect(text).toContain('1 / 1');
    expect(text).toContain('47 discovered');
    expect(text).toContain('10');
    expect(text).toContain('https://one.example/rss');
    http.expectNone('/api/v1/sources');
  });

  it('shows partial feed failures with successful diagnostics', async () => {
    const f = await editor();
    type(f, input(f, 'Feed 1 URL'), 'https://good.example/rss');
    button(f, '+ Add feed').click();
    f.detectChanges();
    type(f, input(f, 'Feed 2 URL'), 'https://bad.example/rss');
    const done = f.componentInstance.test();
    await Promise.resolve();
    http.expectOne('/api/v1/sources/test').flush({
      ok: true,
      mocked: false,
      message: 'Podcast catalog available; audio playback was not tested',
      podcast: {
        feeds_attempted: 2,
        feeds_succeeded: 1,
        discovered_episodes: 180,
        eligible_episodes: 12,
        feeds: [
          { feed_id: 'a', error: null, discovered_episodes: 180 },
          { feed_id: 'b', error: 'PODCAST_FETCH_TIMEOUT', discovered_episodes: 0 },
        ],
      },
    });
    await done;
    f.detectChanges();
    const result = el(f).querySelector('app-source-test-result')!;
    expect(result.querySelector('.warning')).toBeTruthy();
    expect(result.querySelector('.error')).toBeNull();
    expect(result.textContent).toContain('1 of 2 feeds failed');
    expect(result.textContent).toContain('OK — 180 episodes');
    expect(result.textContent).toContain('Timed out');
    expect(result.textContent).toContain('180 discovered');
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
    expect(result.textContent).toContain('Not a usable RSS/Atom feed');
  });

  it('keeps the shared Audio processing panel and no playback controls', async () => {
    const f = await editor();
    const panel = el(f).querySelector('[aria-labelledby="audio-processing-heading"]')!;
    expect(panel.textContent).toContain('Processor');
    expect(panel.textContent).toContain('Source Gain');
    expect(panel.textContent).toContain('Advanced source options');
    expect(el(f).querySelector('[aria-labelledby="source-heading"]')!.contains(panel)).toBe(false);
    for (const word of ['Play', 'Next', 'Previous', 'Skip'])
      expect(
        [...el(f).querySelectorAll('button')].some((b) => b.textContent?.trim() === word),
      ).toBe(false);
  });
});
