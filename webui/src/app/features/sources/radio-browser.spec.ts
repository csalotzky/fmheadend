import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { RadioBrowserPicker } from './radio-browser-picker';
import { RadioBrowserApi, RadioPage, RadioStation } from './radio-browser-api';
import { SourceEditor } from './source-editor';
import { DashboardState } from '../dashboard/dashboard-state';
import { sample } from '../dashboard/test-fixtures';

const root = '/api/v1/radio-browser';
const uuid = '960e57c5-0601-11e8-ae97-52543be04c81';
const station: RadioStation = {
  stationuuid: uuid,
  name: 'Jazz radio',
  url: 'https://example.org/listen.pls',
  url_resolved: 'https://example.org/audio',
  playable_url: 'https://example.org/audio',
  countrycode: 'AT',
  country: 'Austria',
  tags: ['jazz'],
  codec: 'MP3',
  bitrate: 64,
  favicon: null,
  lastcheckok: true,
  hls: false,
};

function setup(id?: number) {
  TestBed.configureTestingModule({
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      provideRouter([]),
      {
        provide: ActivatedRoute,
        useValue: {
          snapshot: { paramMap: convertToParamMap(id === undefined ? {} : { id: String(id) }) },
        },
      },
    ],
  });
  vi.spyOn(TestBed.inject(DashboardState), 'refreshAfterMutation').mockResolvedValue();
  return TestBed.inject(HttpTestingController);
}

describe('Radio Browser modal', () => {
  let http: HttpTestingController;
  beforeEach(() => {
    http = setup();
    vi.useFakeTimers();
  });
  afterEach(() => {
    TestBed.resetTestingModule();
    http.verify();
    vi.useRealTimers();
  });
  async function picker(rows: RadioStation[] = [station]) {
    const f = TestBed.createComponent(RadioBrowserPicker);
    f.detectChanges();
    f.componentInstance.open();
    http
      .expectOne((r) => r.url === root + '/countrycodes')
      .flush([{ name: 'AT', stationcount: 5 }]);
    await vi.advanceTimersByTimeAsync(0);
    const search = http.expectOne((r) => r.url === root + '/stations/search');
    expect(search.request.params.get('offset')).toBe('0');
    expect(search.request.params.get('limit')).toBe('20');
    expect(search.request.params.get('hidebroken')).toBe('true');
    search.flush({ stations: rows, offset: 0, limit: 20 });
    f.detectChanges();
    return f;
  }
  it('opens a modal with bounded default results, country labels and details confirmation', async () => {
    const f = await picker(),
      c = f.componentInstance;
    expect(f.nativeElement.querySelector('dialog').open).toBe(true);
    expect(document.body.style.overflow).toBe('hidden');
    expect(document.activeElement).toBe(f.nativeElement.querySelector('input[type=search]'));
    expect(f.nativeElement.textContent).toContain('Austria');
    const attribution = f.nativeElement.querySelector('.attribution') as HTMLElement;
    expect(attribution.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'Powered by Radio Browser (radio-browser.info)',
    );
    const link = attribution.querySelector('a')!;
    expect(attribution.querySelectorAll('a')).toHaveLength(1);
    expect(link.textContent).toBe('(radio-browser.info)');
    expect(link.getAttribute('href')).toBe('https://www.radio-browser.info/');
    expect(link.target).toBe('_blank');
    expect(link.rel).toBe('noopener noreferrer');
    const chosen = vi.fn();
    c.chosen.subscribe(chosen);
    f.nativeElement.querySelector('.station').click();
    f.detectChanges();
    expect(chosen).not.toHaveBeenCalled();
    expect(f.nativeElement.querySelector('.details').textContent).toContain(station.playable_url);
    c.useStation();
    expect(chosen).toHaveBeenCalledWith(station);
    expect(c.opened()).toBe(false);
    expect(document.body.style.overflow).not.toBe('hidden');
    http.expectNone((r) => r.method !== 'GET');
  });
  it.each(['Cancel', 'Close Radio Browser', 'Escape'])(
    'closes through %s without choosing',
    async (action) => {
      const f = await picker(),
        c = f.componentInstance,
        chosen = vi.fn();
      c.chosen.subscribe(chosen);
      c.selected.set(station);
      f.detectChanges();
      if (action === 'Escape')
        f.nativeElement
          .querySelector('dialog')
          .dispatchEvent(new Event('cancel', { cancelable: true }));
      else {
        const button = [...f.nativeElement.querySelectorAll('button')].find(
          (b: any) => b.textContent.trim() === action || b.getAttribute('aria-label') === action,
        );
        button.click();
      }
      expect(chosen).not.toHaveBeenCalled();
      expect(c.opened()).toBe(false);
    },
  );
  it('debounces name/tag filters, cancels stale searches and resets results', async () => {
    const f = await picker(),
      c = f.componentInstance;
    c.filters.controls.name.setValue('Old');
    await vi.advanceTimersByTimeAsync(299);
    http.expectNone((r) => r.url === root + '/stations/search');
    await vi.advanceTimersByTimeAsync(1);
    const stale = http.expectOne((r) => r.url === root + '/stations/search');
    c.filters.patchValue({ name: 'Jazz', tag: 'jazz', countrycode: 'AT', hidebroken: false });
    expect(stale.cancelled).toBe(true);
    expect(c.stations()).toEqual([]);
    await vi.advanceTimersByTimeAsync(300);
    const current = http.expectOne((r) => r.url === root + '/stations/search');
    expect(
      Object.fromEntries(
        current.request.params.keys().map((k) => [k, current.request.params.get(k)]),
      ),
    ).toEqual({
      name: 'Jazz',
      tag: 'jazz',
      countrycode: 'AT',
      hidebroken: 'false',
      offset: '0',
      limit: '20',
    });
    const tags = http.expectOne((r) => r.url === root + '/tags');
    expect(tags.request.params.get('name')).toBe('jazz');
    expect(tags.request.params.get('limit')).toBe('20');
    tags.flush([{ name: 'jazz', stationcount: 12 }]);
    current.flush({ stations: [station], offset: 0, limit: 20 });
    expect(c.stations()).toEqual([station]);
    expect(c.tags()[0].name).toBe('jazz');
  });
  const rows = (offset: number, count = 20) =>
    Array.from({ length: count }, (_, i) => ({
      ...station,
      stationuuid: String(offset + i),
      name: `Station ${offset + i + 1}`,
    }));
  function respond(offset: number, stations = rows(offset)) {
    const request = http.expectOne((r) => r.url === root + '/stations/search');
    expect(request.request.params.get('offset')).toBe(String(offset));
    expect(request.request.params.get('limit')).toBe('20');
    request.flush({ stations, offset, limit: 20 });
  }
  it('appends two batches, retains cards and prevents concurrent requests without numbered controls', async () => {
    const f = await picker(rows(0)),
      c = f.componentInstance;
    const more = () => f.nativeElement.querySelector('.load-more button') as HTMLButtonElement;
    const firstCard = f.nativeElement.querySelector('.station');
    expect(f.nativeElement.querySelectorAll('.station')).toHaveLength(20);
    expect(f.nativeElement.querySelectorAll('.load-more button')).toHaveLength(1);
    expect(
      f.nativeElement.querySelector('nav, .pagination, .page-numbers, [aria-current="page"]'),
    ).toBeNull();
    expect(
      [...f.nativeElement.querySelectorAll('button')].some((b: any) =>
        /^(Page \d+|Previous page|Next page)$/.test(
          b.getAttribute('aria-label') || b.textContent.trim(),
        ),
      ),
    ).toBe(false);
    for (const offset of [20, 40]) {
      more().click();
      f.detectChanges();
      expect(more().disabled).toBe(true);
      expect(more().textContent).toContain('Loading');
      expect(f.nativeElement.querySelectorAll('.station')).toHaveLength(offset);
      more().click();
      c.loadMore();
      c.retry();
      respond(offset);
      f.detectChanges();
      expect(c.stations()).toEqual(rows(0, offset + 20));
      expect(f.nativeElement.querySelectorAll('.station')).toHaveLength(offset + 20);
      expect(f.nativeElement.querySelector('.station')).toBe(firstCard);
      expect(more().disabled).toBe(false);
      expect(c.opened()).toBe(true);
    }
    http.expectNone((r) => r.url === root + '/stations/search');
    http.expectNone((r) => r.method !== 'GET');
  });
  it.each([0, 7])('hides Load more after an appended batch of %s stations', async (count) => {
    const f = await picker(rows(0)),
      c = f.componentInstance;
    c.loadMore();
    respond(20, rows(20, count));
    f.detectChanges();
    expect(c.stations()).toEqual(rows(0, 20 + count));
    expect(f.nativeElement.querySelector('.load-more')).toBeNull();
    expect(f.nativeElement.textContent).not.toContain('No stations found');
    c.loadMore();
    http.expectNone((r) => r.url === root + '/stations/search');
  });
  it.each([0, 7])('hides Load more after an initial batch of %s stations', async (count) => {
    const f = await picker(rows(0, count));
    expect(f.nativeElement.querySelector('.load-more')).toBeNull();
    expect(f.componentInstance.hasMore()).toBe(false);
  });
  it('preserves results and selection on failure and retries the same offset', async () => {
    const f = await picker(rows(0)),
      c = f.componentInstance;
    c.selected.set(c.stations()[0]);
    c.loadMore();
    const failed = http.expectOne((r) => r.url === root + '/stations/search');
    expect(failed.request.params.get('offset')).toBe('20');
    failed.flush({ message: 'Directory unavailable' }, { status: 503, statusText: 'Unavailable' });
    f.detectChanges();
    expect(c.stations()).toEqual(rows(0));
    expect(f.nativeElement.querySelectorAll('.station')).toHaveLength(20);
    expect(c.selected()).toEqual(rows(0)[0]);
    expect(f.nativeElement.querySelector('[role="alert"]').textContent).toContain(
      'Directory unavailable',
    );
    expect(f.nativeElement.querySelector('.load-more button').disabled).toBe(false);
    f.nativeElement.querySelector('[role="alert"] button').click();
    respond(20);
    expect(c.stations()).toEqual(rows(0, 40));
    expect(c.error()).toBe('');
    c.loadMore();
    respond(40);
  });
  it('retries an initial search failure at offset zero', async () => {
    const f = await picker();
    f.componentInstance.filters.controls.name.setValue('Retry');
    await vi.advanceTimersByTimeAsync(300);
    http
      .expectOne((r) => r.url === root + '/stations/search')
      .flush({ message: 'Unavailable' }, { status: 503, statusText: 'Unavailable' });
    f.componentInstance.retry();
    respond(0);
    expect(f.componentInstance.stations()).toEqual(rows(0));
    expect(f.componentInstance.hasMore()).toBe(true);
  });
  it.each(['name', 'countrycode', 'tag', 'hidebroken'] as const)(
    'resets results and offset and cancels an old batch when %s changes',
    async (key) => {
      const f = await picker(rows(0)),
        c = f.componentInstance;
      c.loadMore();
      respond(20);
      c.loadMore();
      const stale = http.expectOne((r) => r.url === root + '/stations/search');
      c.filters.patchValue({
        [key]: key === 'hidebroken' ? false : key === 'countrycode' ? 'AT' : 'jazz',
      });
      expect(stale.cancelled).toBe(true);
      expect(c.hasMore()).toBe(false);
      expect(c.stations()).toEqual([]);
      c.loadMore();
      await vi.advanceTimersByTimeAsync(299);
      http.expectNone((r) => r.url === root + '/stations/search');
      await vi.advanceTimersByTimeAsync(1);
      respond(0);
      for (const req of http.match((r) => r.url === root + '/tags')) req.flush([]);
      expect(c.stations()).toEqual(rows(0));
      expect(c.hasMore()).toBe(true);
      c.loadMore();
      respond(20);
      expect(c.stations()).toEqual(rows(0, 40));
    },
  );
  it('ignores late success and error callbacks from an invalidated query', async () => {
    const f = await picker(rows(0)),
      c = f.componentInstance;
    let old!: { next: (batch: RadioPage) => void; error: (error: unknown) => void };
    const unsubscribe = vi.fn();
    // Deliberately deliver callbacks even after cancellation to exercise the generation guard.
    vi.spyOn(TestBed.inject(RadioBrowserApi), 'search').mockReturnValueOnce({
      subscribe: (observer: typeof old) => {
        old = observer;
        return { unsubscribe };
      },
    } as any);
    c.loadMore();
    c.filters.controls.name.setValue('New');
    expect(unsubscribe).toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(300);
    respond(0, rows(100));
    old.next({ stations: rows(20), offset: 20, limit: 20 });
    old.error(new Error('Old request failed'));
    expect(c.stations()).toEqual(rows(100));
    expect(c.error()).toBe('');
    expect(c.hasMore()).toBe(true);
    expect(c.loading()).toBe(false);
  });
  it('preserves scroll position and selection when appending and confirms only on Use station', async () => {
    const f = await picker(rows(0)),
      c = f.componentInstance,
      chosen = vi.fn();
    c.chosen.subscribe(chosen);
    f.nativeElement.querySelector('.station').click();
    f.detectChanges();
    const results = f.nativeElement.querySelector('.results') as HTMLElement;
    results.scrollTop = 450;
    const scroll = vi.spyOn(window, 'scrollTo');
    c.loadMore();
    f.detectChanges();
    expect(results.scrollTop).toBe(450);
    expect(c.selected()).toEqual(rows(0)[0]);
    c.useStation();
    expect(chosen).not.toHaveBeenCalled();
    respond(20);
    f.detectChanges();
    expect(results.scrollTop).toBe(450);
    expect(c.opened()).toBe(true);
    expect(scroll).not.toHaveBeenCalled();
    expect(c.selected()).toEqual(rows(0)[0]);
    const details = f.nativeElement.querySelector('.details').textContent;
    expect(details).toContain('MP3');
    expect(details).toContain('64 kb/s');
    const use = [...f.nativeElement.querySelectorAll('button')].find(
      (b: any) => b.textContent.trim() === 'Use station',
    );
    expect(use.disabled).toBe(false);
    use.click();
    expect(chosen).toHaveBeenCalledExactlyOnceWith(rows(0)[0]);
    expect(c.opened()).toBe(false);
  });
  it('renders empty, broken and unsupported results without requiring optional metadata', async () => {
    const f = await picker([]),
      c = f.componentInstance;
    expect(f.nativeElement.textContent).toContain('No stations found');
    c.stations.set([
      {
        ...station,
        name: '',
        country: '',
        countrycode: '',
        codec: '',
        bitrate: 0,
        tags: [],
        playable_url: null,
        lastcheckok: false,
      },
    ]);
    c.selected.set(c.stations()[0]);
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('Reported broken');
    const use = [...f.nativeElement.querySelectorAll('button')].find(
      (b: any) => b.textContent.trim() === 'Use station',
    );
    expect(use.disabled).toBe(true);
  });
  it('reports independent country/tag failures and cancels outstanding requests on close', async () => {
    const f = TestBed.createComponent(RadioBrowserPicker);
    f.detectChanges();
    const c = f.componentInstance;
    c.open();
    http
      .expectOne((r) => r.url === root + '/countrycodes')
      .flush({ message: 'Countries unavailable' }, { status: 503, statusText: 'Unavailable' });
    c.filters.controls.tag.setValue('jazz');
    await vi.advanceTimersByTimeAsync(300);
    const search = http.expectOne((r) => r.url === root + '/stations/search');
    http
      .expectOne((r) => r.url === root + '/tags')
      .flush({ message: 'Tags unavailable' }, { status: 503, statusText: 'Unavailable' });
    expect(c.countriesError()).toContain('Countries unavailable');
    expect(c.tagsError()).toContain('Tags unavailable');
    c.close();
    expect(search.cancelled).toBe(true);
  });
  it('restores opener focus and scroll state on destruction', async () => {
    const button = document.createElement('button');
    document.body.appendChild(button);
    button.focus();
    document.body.style.overflow = 'auto';
    const f = await picker();
    f.destroy();
    expect(document.activeElement).toBe(button);
    expect(document.body.style.overflow).toBe('auto');
    button.remove();
    document.body.style.overflow = '';
  });
});

describe('HTTP Source Radio Browser draft', () => {
  let http: HttpTestingController;
  afterEach(() => {
    TestBed.resetTestingModule();
    http.verify();
  });
  async function editor(linked = false) {
    http = setup(linked ? 1 : undefined);
    const f = TestBed.createComponent(SourceEditor);
    f.detectChanges();
    http.expectOne('/api/v1/audio-profiles/files').flush([]);
    const saved = {
      ...sample().sources[0],
      type: 'HTTP' as const,
      name: 'Existing name',
      timestamp: 'revision-1',
      config: { uri: 'https://old.example/live', radio_browser_uuid: uuid },
    };
    if (linked) http.expectOne('/api/v1/sources/1').flush(saved);
    await f.whenStable();
    f.detectChanges();
    return { f, c: f.componentInstance, saved };
  }
  it('shows the browse link only for HTTP and leaves ordinary URLs editable', async () => {
    const { f, c } = await editor();
    expect(f.nativeElement.querySelector('#source-uri').readOnly).toBe(false);
    expect(f.nativeElement.textContent).toContain('Browse stations');
    c.form.controls.type.setValue('UDP');
    c.changeType();
    f.detectChanges();
    expect(f.nativeElement.textContent).not.toContain('Browse stations');
  });
  it('opens above the editor without navigation or resetting the draft', async () => {
    const { f, c } = await editor();
    c.form.controls.name.setValue('My draft');
    c.form.markAsDirty();
    const nav = vi.spyOn(TestBed.inject(Router), 'navigate');
    c.openRadioBrowser();
    http.expectOne((r) => r.url === root + '/countrycodes').flush([]);
    c.radioPicker!.selected.set(station);
    c.radioPicker!.close();
    expect(c.form.controls.name.value).toBe('My draft');
    expect(c.dirty()).toBe(true);
    expect(c.radioUuid.value).toBeNull();
    expect(nav).not.toHaveBeenCalled();
    http.expectNone((r) => r.method !== 'GET');
    expect(f.nativeElement.querySelector('app-radio-browser-picker').closest('form')).toBeNull();
  });
  it.each(['', 'User name'])(
    'applies URL and UUID without saving and preserves Name %s',
    async (name) => {
      const { c } = await editor();
      c.form.controls.name.setValue(name);
      c.useRadioStation(station);
      expect(c.draft()?.config).toEqual({ uri: station.playable_url, radio_browser_uuid: uuid });
      expect(c.form.controls.name.value).toBe(name || station.name);
      expect(c.dirty()).toBe(true);
      vi.spyOn(window, 'confirm').mockReturnValue(false);
      expect(c.canLeave()).toBe(false);
      http.expectNone((r) => r.method !== 'GET');
    },
  );
  it('keeps linked URLs read-only while testing the current draft', async () => {
    const { f, c } = await editor(true);
    expect(f.nativeElement.querySelector('#source-uri').readOnly).toBe(true);
    expect(f.nativeElement.textContent).toContain('Radio Browser linked');
    const pending = c.test();
    const req = http.expectOne('/api/v1/sources/test');
    expect(req.request.body.config.radio_browser_uuid).toBe(uuid);
    req.flush({ ok: true, mocked: false, message: 'Connected' });
    await pending;
    expect(c.dirty()).toBe(false);
  });
  it('refreshes the draft only, reports unchanged URLs and retains URL on deletion/outage', async () => {
    const { c } = await editor(true);
    const pending = c.refreshRadioUrl();
    http.expectOne(root + '/stations/' + uuid).flush(station);
    await pending;
    expect(c.config.controls['uri'].value).toBe(station.playable_url);
    expect(c.dirty()).toBe(true);
    c.config.markAsPristine();
    const same = c.refreshRadioUrl();
    http.expectOne(root + '/stations/' + uuid).flush(station);
    await same;
    expect(c.radioMessage()).toContain('unchanged');
    expect(c.dirty()).toBe(false);
    for (const status of [404, 503]) {
      const fail = c.refreshRadioUrl();
      http
        .expectOne(root + '/stations/' + uuid)
        .flush({ message: 'Unavailable' }, { status, statusText: 'Unavailable' });
      await fail;
      expect(c.radioError()).toBe('Unavailable');
      expect(c.config.controls['uri'].value).toBe(station.playable_url);
      expect(c.radioUuid.value).toBe(uuid);
    }
    http.expectNone((r) => r.method !== 'GET');
  });
  it('unlinks without saving and ignores a late refresh response', async () => {
    const { f, c, saved } = await editor(true);
    const pending = c.refreshRadioUrl();
    const req = http.expectOne(root + '/stations/' + uuid);
    c.unlinkRadio();
    req.flush(station);
    await pending;
    f.detectChanges();
    expect(c.config.controls['uri'].value).toBe(saved.config.uri);
    expect(c.radioUuid.value).toBeNull();
    expect(c.draft()?.config['radio_browser_uuid']).toBeNull();
    expect(c.dirty()).toBe(true);
    expect(f.nativeElement.querySelector('#source-uri').readOnly).toBe(false);
    http.expectNone((r) => r.method !== 'GET');
  });
  it('saves a selected UUID through the normal create path and clears dirty state', async () => {
    const { c } = await editor();
    c.useRadioStation(station);
    const pending = c.save();
    const req = http.expectOne('/api/v1/sources');
    expect(req.request.method).toBe('POST');
    expect(req.request.body.config).toEqual({
      uri: station.playable_url,
      radio_browser_uuid: uuid,
    });
    req.flush({ ...sample().sources[0], ...req.request.body, timestamp: 'new', applied: true });
    await pending;
    expect(c.dirty()).toBe(false);
    expect(c.message()).toBe('Source saved.');
  });
  it('saves unlink through the normal patch after checking the saved revision', async () => {
    const { c, saved } = await editor(true);
    c.unlinkRadio();
    const pending = c.save();
    http.expectOne('/api/v1/sources/1').flush(saved);
    await Promise.resolve();
    const req = http.expectOne('/api/v1/sources/1');
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body.config).toEqual({ uri: saved.config.uri, radio_browser_uuid: null });
    req.flush({ ...saved, ...req.request.body });
    await pending;
    expect(c.dirty()).toBe(false);
  });
  it('blocks a stale save after recovery without losing unrelated edits', async () => {
    const { c, saved } = await editor(true);
    c.form.controls.name.setValue('Unsaved name');
    c.form.markAsDirty();
    const pending = c.save();
    http.expectOne('/api/v1/sources/1').flush({
      ...saved,
      timestamp: 'revision-2',
      config: { ...saved.config, uri: station.playable_url },
    });
    await pending;
    expect(c.sourceConflict()).toBe(true);
    expect(c.error()).toContain('saved Source changed');
    expect(c.form.controls.name.value).toBe('Unsaved name');
    expect(c.dirty()).toBe(true);
    expect(c.saving()).toBe(false);
    http.expectNone((r) => r.method === 'PATCH');
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    await c.reloadSource();
    http.expectNone('/api/v1/sources/1');
    expect(c.form.controls.name.value).toBe('Unsaved name');
  });
  it('browsing and loading more change the Source draft only on Use station', async () => {
    const { f, c } = await editor(true);
    const before = c.draft();
    c.openRadioBrowser();
    http.expectOne((r) => r.url === root + '/countrycodes').flush([]);
    await new Promise((resolve) => setTimeout(resolve, 5));
    const rows = Array.from({ length: 20 }, (_, i) => ({ ...station, stationuuid: String(i) }));
    http
      .expectOne((r) => r.url === root + '/stations/search')
      .flush({ stations: rows, offset: 0, limit: 20 });
    c.radioPicker!.selected.set(rows[0]);
    c.radioPicker!.loadMore();
    http
      .expectOne((r) => r.url === root + '/stations/search')
      .flush({ stations: [station], offset: 20, limit: 20 });
    f.detectChanges();
    expect(c.draft()).toEqual(before);
    expect(c.dirty()).toBe(false);
    expect(c.radioPicker!.selected()).toEqual(rows[0]);
    http.expectNone((r) => r.method !== 'GET');
    c.radioPicker!.filters.controls.name.setValue('New search');
    expect(c.draft()).toEqual(before);
    await new Promise((resolve) => setTimeout(resolve, 320));
    const search = http.expectOne((r) => r.url === root + '/stations/search');
    expect(search.request.params.get('offset')).toBe('0');
    search.flush({ stations: [station], offset: 0, limit: 20 });
    c.radioPicker!.selected.set(station);
    expect(c.draft()).toEqual(before);
    c.radioPicker!.useStation();
    expect(c.draft()?.config).toEqual({ uri: station.playable_url, radio_browser_uuid: uuid });
    expect(c.dirty()).toBe(true);
    http.expectNone((r) => r.method !== 'GET');
  });
  it('does not overwrite drafts with dashboard snapshots', async () => {
    const { c, saved } = await editor(true);
    c.form.controls.name.setValue('Draft');
    c.form.markAsDirty();
    const snapshot = sample();
    snapshot.sources = [{ ...saved, config: { ...saved.config, uri: station.playable_url } }];
    TestBed.inject(DashboardState).data.set(snapshot);
    expect(c.config.controls['uri'].value).toBe(saved.config.uri);
    expect(c.form.controls.name.value).toBe('Draft');
  });
});
