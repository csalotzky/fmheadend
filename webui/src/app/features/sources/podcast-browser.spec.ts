import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { Observable, Observer } from 'rxjs';
import { DashboardState } from '../dashboard/dashboard-state';
import { sample } from '../dashboard/test-fixtures';
import {
  PodcastBrowserApi,
  PodcastSearchResponse,
  PodcastSearchResult,
} from './podcast-browser-api';
import { PodcastBrowserPicker } from './podcast-browser-picker';
import { SourceEditor } from './source-editor';

const searchUrl = '/api/v1/podcast-browser/search';
const rows = (count: number): PodcastSearchResult[] =>
  Array.from({ length: count }, (_, i) => ({
    title: `Podcast ${i + 1}`,
    publisher: 'Publisher',
    category: 'News',
    episode_count: i,
    artwork_url: `https://covers.example.org/${i}.jpg`,
    feed_url: `https://feeds.example.org/${i}.xml`,
  }));

function setup(id?: number) {
  TestBed.configureTestingModule({
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      provideRouter([]),
      {
        provide: ActivatedRoute,
        useValue: { snapshot: { paramMap: convertToParamMap(id ? { id } : {}) } },
      },
    ],
  });
  vi.spyOn(TestBed.inject(DashboardState), 'refreshAfterMutation').mockResolvedValue();
  return TestBed.inject(HttpTestingController);
}

function element(f: ComponentFixture<unknown>): HTMLElement {
  return f.nativeElement;
}

function findButton(f: ComponentFixture<unknown>, label: string) {
  return [...element(f).querySelectorAll('button')].find(
    (button) => button.textContent.trim() === label,
  )!;
}

function searchInput(f: ComponentFixture<unknown>) {
  return element(f).querySelector<HTMLInputElement>('dialog input[type=search]')!;
}

function typeSearch(f: ComponentFixture<unknown>, name: string) {
  const input = searchInput(f);
  input.value = name;
  input.dispatchEvent(new Event('input'));
  f.detectChanges();
}

function expectSearch(http: HttpTestingController, name: string) {
  const request = http.expectOne((r) => r.url === searchUrl);
  expect(request.request.method).toBe('GET');
  expect(request.request.params.keys()).toEqual(['name']);
  expect(request.request.params.get('name')).toBe(name);
  return request;
}

describe('Podcast Browser picker', () => {
  let http: HttpTestingController;
  beforeEach(() => {
    http = setup();
    vi.useFakeTimers();
  });
  afterEach(() => {
    TestBed.resetTestingModule();
    vi.useRealTimers();
    http.verify();
  });

  function picker() {
    const f = TestBed.createComponent(PodcastBrowserPicker);
    f.detectChanges();
    f.componentInstance.open();
    f.detectChanges();
    return f;
  }

  it('opens with one focused field, skips blank input, and debounces a trimmed title for 800 ms', async () => {
    const f = picker();
    const dialog = element(f).querySelector('dialog')!;
    expect(dialog.open).toBe(true);
    expect(document.activeElement).toBe(searchInput(f));
    expect(dialog.querySelectorAll('input, select')).toHaveLength(1);
    typeSearch(f, '   ');
    await vi.advanceTimersByTimeAsync(900);
    http.expectNone((r) => r.url === searchUrl);
    typeSearch(f, 'Initial');
    await vi.advanceTimersByTimeAsync(700);
    typeSearch(f, '  Árvíz & hírek  ');
    expect(dialog.textContent).toContain('Searching podcasts');
    await vi.advanceTimersByTimeAsync(799);
    http.expectNone((r) => r.url === searchUrl);
    await vi.advanceTimersByTimeAsync(1);
    expectSearch(http, 'Árvíz & hírek').flush({ podcasts: [] });
    f.detectChanges();
    expect(dialog.textContent).toContain('No podcasts found');
    typeSearch(f, 'Árvíz & hírek');
    await vi.advanceTimersByTimeAsync(800);
    http.expectNone((r) => r.url === searchUrl);
  });

  it('reveals 20 results at a time without fetching again and resets the result pane for a new query', async () => {
    const f = picker();
    typeSearch(f, 'Catalog');
    await vi.advanceTimersByTimeAsync(800);
    expectSearch(http, 'Catalog').flush({ podcasts: rows(41) });
    f.detectChanges();
    const results = element(f).querySelector<HTMLElement>('#podcast-results')!;
    const titles = () => [...results.querySelectorAll('h3')].map((h) => h.textContent);
    expect(titles()).toHaveLength(20);
    results.scrollTop = 250;
    findButton(f, 'Load more').click();
    f.detectChanges();
    expect(titles()).toHaveLength(40);
    expect(results.scrollTop).toBe(250);
    findButton(f, 'Load more').click();
    f.detectChanges();
    expect(titles()).toEqual(rows(41).map((row) => row.title));
    expect(findButton(f, 'Load more')).toBeUndefined();
    http.expectNone((r) => r.url === searchUrl);
    typeSearch(f, 'Other');
    expect(titles()).toEqual([]);
    expect(results.scrollTop).toBe(0);
    await vi.advanceTimersByTimeAsync(800);
    expectSearch(http, 'Other').flush({ podcasts: rows(21) });
    f.detectChanges();
    expect(titles()).toHaveLength(20);
  });

  it('renders catalog metadata, remote cover URLs, and missing or broken cover fallbacks', async () => {
    const f = picker();
    const [first, second] = rows(2);
    typeSearch(f, 'News');
    await vi.advanceTimersByTimeAsync(800);
    expectSearch(http, 'News').flush({
      podcasts: [first, { ...second, artwork_url: null, episode_count: null }],
    });
    f.detectChanges();
    const [covered, noCover] = element(f).querySelectorAll('article');
    const image = covered.querySelector('img')!;
    expect(image.getAttribute('src')).toBe(first.artwork_url);
    expect(image.getAttribute('loading')).toBe('lazy');
    expect(image.getAttribute('referrerpolicy')).toBe('no-referrer');
    for (const text of [
      first.title,
      first.publisher,
      first.category,
      'Episodes (Apple catalog): 0',
    ]) {
      expect(covered.textContent).toContain(text);
    }
    expect(noCover.textContent).toContain('No cover');
    expect(noCover.textContent).not.toContain('Episodes (Apple catalog)');
    image.dispatchEvent(new Event('error'));
    f.detectChanges();
    expect(covered.querySelector('img')).toBeNull();
    expect(covered.textContent).toContain('No cover');
    http.expectNone(() => true);
  });

  it('shows search errors and retries the same title only when requested', async () => {
    const f = picker();
    typeSearch(f, 'News');
    await vi.advanceTimersByTimeAsync(800);
    expectSearch(http, 'News').flush(
      { message: 'Apple Podcasts search is temporarily unavailable.' },
      { status: 503, statusText: 'Unavailable' },
    );
    f.detectChanges();
    expect(element(f).querySelector('[role=alert]')!.textContent).toContain(
      'temporarily unavailable',
    );
    expect(element(f).textContent).not.toContain('No podcasts found');
    await vi.advanceTimersByTimeAsync(5000);
    http.expectNone((r) => r.url === searchUrl);
    findButton(f, 'Retry search').click();
    expectSearch(http, 'News').flush({ podcasts: rows(1) });
    f.detectChanges();
    expect(element(f).querySelector('[role=alert]')).toBeNull();
    expect(element(f).querySelector('article')!.textContent).toContain('Podcast 1');
  });

  it('cancels pending work on edit, clear and close, and restores opener focus', async () => {
    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();
    const previousOverflow = document.body.style.overflow;
    try {
      const f = picker();
      const dialog = element(f).querySelector('dialog')!;
      typeSearch(f, 'Old');
      await vi.advanceTimersByTimeAsync(800);
      const old = expectSearch(http, 'Old');
      typeSearch(f, 'New');
      expect(old.cancelled).toBe(true);
      await vi.advanceTimersByTimeAsync(800);
      const current = expectSearch(http, 'New');
      typeSearch(f, '');
      expect(current.cancelled).toBe(true);
      typeSearch(f, 'Pending');
      await vi.advanceTimersByTimeAsync(800);
      const pending = expectSearch(http, 'Pending');
      dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
      expect(pending.cancelled).toBe(true);
      expect(dialog.open).toBe(false);
      expect(document.activeElement).toBe(opener);
      expect(document.body.style.overflow).toBe(previousOverflow);
      f.componentInstance.open();
      expect(searchInput(f).value).toBe('');
      typeSearch(f, 'Still debouncing');
      findButton(f, 'Close').click();
      await vi.advanceTimersByTimeAsync(800);
      http.expectNone((r) => r.url === searchUrl);
    } finally {
      opener.remove();
    }
  });

  it('ignores stale success and error callbacks even after a new search succeeds', async () => {
    const f = picker();
    let stale!: Pick<Observer<PodcastSearchResponse>, 'next' | 'error'>;
    const unsubscribe = vi.fn();
    vi.spyOn(TestBed.inject(PodcastBrowserApi), 'search').mockReturnValueOnce({
      subscribe: (observer: typeof stale) => {
        stale = observer;
        return { unsubscribe };
      },
    } as unknown as Observable<PodcastSearchResponse>);
    typeSearch(f, 'Old');
    await vi.advanceTimersByTimeAsync(800);
    typeSearch(f, 'New');
    expect(unsubscribe).toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(800);
    expectSearch(http, 'New').flush({ podcasts: [{ ...rows(1)[0], title: 'Current result' }] });
    stale.next({ podcasts: rows(41) });
    stale.error(new Error('Stale failure'));
    f.detectChanges();
    expect(element(f).querySelectorAll('article')).toHaveLength(1);
    expect(element(f).querySelector('article')!.textContent).toContain('Current result');
    expect(element(f).querySelector('[role=alert]')).toBeNull();
  });
});

describe('Podcast Browser in Source Editor', () => {
  let http: HttpTestingController;
  const feed = (
    url: string,
    max_age_days: number | null = null,
    max_episodes: number | null = null,
  ) => ({
    url,
    max_age_days,
    max_episodes,
  });
  type Feed = ReturnType<typeof feed>;

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.useRealTimers();
    http.verify();
  });

  async function editor(feeds?: Feed[]) {
    http = setup(feeds ? 1 : undefined);
    const f = TestBed.createComponent(SourceEditor);
    const c = f.componentInstance;
    f.detectChanges();
    http.expectOne('/api/v1/audio-profiles/files').flush([]);
    if (feeds) {
      http.expectOne('/api/v1/sources/1').flush({
        ...sample().sources[0],
        type: 'PODCAST',
        config: { feeds, schedule_mode: 'RANDOM', refresh_interval_minutes: 90 },
      });
    } else {
      expect(findButton(f, 'Browse podcasts')).toBeUndefined();
      c.form.controls.type.setValue('PODCAST');
      c.changeType();
      c.form.controls.name.setValue('New source');
    }
    await f.whenStable();
    f.detectChanges();
    vi.useFakeTimers();
    return f;
  }

  async function browse(f: ComponentFixture<SourceEditor>, podcasts: PodcastSearchResult[]) {
    const browse = findButton(f, 'Browse podcasts');
    expect(browse.parentElement!.querySelector('#podcast-feeds-label')!.textContent).toBe('Feeds');
    browse.click();
    f.detectChanges();
    typeSearch(f, 'News');
    await vi.advanceTimersByTimeAsync(800);
    expectSearch(http, 'News').flush({ podcasts });
    f.detectChanges();
  }

  const addButtons = (f: ComponentFixture<SourceEditor>) => [
    ...element(f).querySelectorAll<HTMLButtonElement>('article button'),
  ];

  it('fills the starter row, marks the form dirty and keeps the picker open without Save or Test', async () => {
    const f = await editor();
    const c = f.componentInstance;
    const [podcast] = rows(1);
    await browse(f, [podcast]);
    const button = addButtons(f)[0];
    button.click();
    button.click();
    f.detectChanges();
    expect(c.feeds.getRawValue()).toEqual([feed(podcast.feed_url)]);
    expect(c.feeds.at(0).controls.url.dirty).toBe(true);
    expect(c.podcast.dirty).toBe(true);
    expect(c.podcast.valid).toBe(true);
    expect(element(f).querySelector('app-podcast-browser-picker dialog[open]')).not.toBeNull();
    expect(element(f).querySelector('.confirmation')!.textContent).toContain(
      'Podcast added to source',
    );
    expect(button.textContent.trim()).toBe('Added');
    expect(button.disabled).toBe(true);
    http.expectNone(() => true);
  });

  it('fills the first blank row, preserves its limits and ordering, then appends to the unsaved draft', async () => {
    const first = feed('https://first.example.org/rss', 21);
    const last = feed('https://last.example.org/rss', null, 2);
    const f = await editor([first, feed('https://middle.example.org/rss', 5, 7), last]);
    const c = f.componentInstance;
    c.form.controls.name.setValue('Unsaved name');
    c.feeds.at(1).controls.url.setValue('   ');
    const [fill, append] = rows(2);
    await browse(f, [fill, append]);
    const before = c.podcast.getRawValue();
    const buttons = addButtons(f);
    buttons[0].click();
    buttons[1].click();
    f.detectChanges();
    const expected = {
      ...before,
      feeds: [first, feed(fill.feed_url, 5, 7), last, feed(append.feed_url)],
    };
    expect(c.podcast.getRawValue()).toEqual(expected);
    expect(c.form.controls.name.value).toBe('Unsaved name');
    expect(c.podcast.dirty).toBe(true);
    http.expectNone(() => true);
    findButton(f, 'Close').click();
    const done = c.save();
    const request = http.expectOne('/api/v1/sources/1');
    expect(request.request.method).toBe('PATCH');
    expect(request.request.body.config).toEqual(expected);
    expect(request.request.body.name).toBe('Unsaved name');
    request.flush({ ...sample().sources[0], id: 1 });
    await done;
    expect(c.podcast.dirty).toBe(false);
  });

  it('tracks saved, edited and removed URLs including trimmed duplicates after reopening', async () => {
    const [already, other] = rows(2);
    const f = await editor([feed(already.feed_url)]);
    const c = f.componentInstance;
    await browse(f, [already, other]);
    expect(addButtons(f).map((b) => b.textContent.trim())).toEqual(['Added', 'Add podcast']);
    findButton(f, 'Close').click();
    c.feeds.at(0).controls.url.setValue(`  ${other.feed_url}  `);
    await browse(f, [already, other]);
    expect(addButtons(f).map((b) => b.textContent.trim())).toEqual(['Add podcast', 'Added']);
    c.addPodcastFeed(other);
    expect(c.feeds.length).toBe(1);
    addButtons(f)[0].click();
    f.detectChanges();
    expect(c.feeds.length).toBe(2);
    c.removeFeed(0);
    f.detectChanges();
    expect(addButtons(f).map((b) => b.textContent.trim())).toEqual(['Added', 'Add podcast']);
    http.expectNone(() => true);
  });

  it('fills a blank at 32 rows and blocks additional feeds once all rows are populated', async () => {
    const existing = rows(32).map((row) => feed(row.feed_url));
    const f = await editor(existing);
    const c = f.componentInstance;
    const [fill, extra] = rows(34).slice(32);
    await browse(f, [fill, extra]);
    expect(addButtons(f).every((button) => button.disabled)).toBe(true);
    expect(element(f).textContent).toContain('up to 32 feeds');
    c.addPodcastFeed(extra);
    expect(c.feeds.getRawValue()).toEqual(existing);
    c.feeds.at(10).controls.url.setValue('');
    f.detectChanges();
    expect(addButtons(f).every((button) => !button.disabled)).toBe(true);
    addButtons(f)[0].click();
    f.detectChanges();
    expect(c.feeds.length).toBe(32);
    expect(c.feeds.at(10).controls.url.value).toBe(fill.feed_url);
    expect(addButtons(f).map((button) => button.textContent.trim())).toEqual([
      'Added',
      'Add podcast',
    ]);
    expect(addButtons(f).every((button) => button.disabled)).toBe(true);
    http.expectNone(() => true);
  });
});
