import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { RadioBrowserPicker } from './radio-browser-picker';
import { RadioStation } from './radio-browser-api';
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
  it('debounces name/tag filters, cancels stale searches and resets pagination', async () => {
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
  const rows = (page: number) =>
    Array.from({ length: 20 }, (_, i) => ({
      ...station,
      stationuuid: `${page}-${i}`,
      name: `Page ${page} station ${i}`,
    }));
  function respond(page: number, stations = rows(page)) {
    const request = http.expectOne((r) => r.url === root + '/stations/search');
    expect(request.request.params.get('offset')).toBe(String((page - 1) * 20));
    expect(request.request.params.get('limit')).toBe('20');
    request.flush({ stations, offset: (page - 1) * 20, limit: 20 });
  }
  it('replaces pages, synchronizes both controls, and supports numeric and Previous navigation', async () => {
    const f = await picker(rows(1)),
      c = f.componentInstance;
    const navs = () => [...f.nativeElement.querySelectorAll('nav.pagination')] as HTMLElement[];
    const buttons = (label: string) =>
      navs().map((nav) => nav.querySelector(`[aria-label="${label}"]`) as HTMLButtonElement);
    expect(navs()).toHaveLength(2);
    expect(f.nativeElement.querySelectorAll('.station')).toHaveLength(20);
    expect(buttons('Previous page').every((b) => b.disabled)).toBe(true);
    expect(c.pages()).toEqual([1, 2]);
    buttons('Next page')[0].click();
    f.detectChanges();
    expect(buttons('Next page').every((b) => b.disabled)).toBe(true);
    expect([...f.nativeElement.querySelectorAll('.station')].every((b: any) => b.disabled)).toBe(
      true,
    );
    respond(2);
    f.detectChanges();
    expect(c.stations()).toEqual(rows(2));
    expect(f.nativeElement.querySelectorAll('.station')).toHaveLength(20);
    expect(
      navs().every(
        (nav) => nav.querySelector('[aria-current="page"]')?.textContent?.trim() === '2',
      ),
    ).toBe(true);
    expect(c.pages()).toEqual([1, 2, 3]);
    buttons('Page 3')[1].click();
    respond(3, [station]);
    f.detectChanges();
    expect(c.page()).toBe(3);
    expect(buttons('Next page').every((b) => b.disabled)).toBe(true);
    buttons('Previous page')[1].click();
    respond(2);
    f.detectChanges();
    buttons('Page 1')[0].click();
    respond(1);
    f.detectChanges();
    expect(c.page()).toBe(1);
    expect(c.pages()).toEqual([1, 2, 3]);
    expect(f.nativeElement.textContent).not.toContain('Load more');
    http.expectNone((r) => r.method !== 'GET');
  });
  it('keeps the nonempty page when optimistic Next finds the exact end', async () => {
    const f = await picker(rows(1)),
      c = f.componentInstance;
    c.goToPage(2);
    respond(2, []);
    f.detectChanges();
    expect(c.page()).toBe(1);
    expect(c.stations()).toEqual(rows(1));
    expect(c.pages()).toEqual([1]);
    expect(
      [...f.nativeElement.querySelectorAll('[aria-label="Next page"]')].every(
        (b: any) => b.disabled,
      ),
    ).toBe(true);
    expect(f.nativeElement.textContent).not.toContain('No stations found');
    c.goToPage(2);
    http.expectNone((r) => r.url === root + '/stations/search');
  });
  it('retains the displayed page on errors and retries the requested page', async () => {
    const f = await picker(rows(1)),
      c = f.componentInstance;
    c.goToPage(2);
    http
      .expectOne((r) => r.url === root + '/stations/search')
      .flush({ message: 'Directory unavailable' }, { status: 503, statusText: 'Unavailable' });
    expect(c.page()).toBe(1);
    expect(c.stations()).toEqual(rows(1));
    expect(c.error()).toContain('Directory unavailable');
    c.retry();
    respond(2);
    expect(c.stations()).toEqual(rows(2));
    expect(c.page()).toBe(2);
  });
  it.each(['name', 'countrycode', 'tag', 'hidebroken'] as const)(
    'resets history and cancels old pages when %s changes',
    async (key) => {
      const f = await picker(rows(1)),
        c = f.componentInstance;
      c.goToPage(2);
      respond(2);
      c.goToPage(3);
      const stale = http.expectOne((r) => r.url === root + '/stations/search');
      c.filters.patchValue({
        [key]: key === 'hidebroken' ? false : key === 'countrycode' ? 'AT' : 'jazz',
      });
      expect(stale.cancelled).toBe(true);
      expect(c.page()).toBe(1);
      expect(c.pages()).toEqual([1]);
      expect(c.stations()).toEqual([]);
      await vi.advanceTimersByTimeAsync(299);
      http.expectNone((r) => r.url === root + '/stations/search');
      await vi.advanceTimersByTimeAsync(1);
      respond(1, [station]);
      for (const req of http.match((r) => r.url === root + '/tags')) req.flush([]);
      expect(c.pages()).toEqual([1]);
      expect(c.stations()).toEqual([station]);
    },
  );
  it('scrolls only the results region and clears selection without emitting a choice', async () => {
    const f = await picker(rows(1)),
      c = f.componentInstance,
      chosen = vi.fn();
    c.chosen.subscribe(chosen);
    c.selected.set(c.stations()[0]);
    c.results.nativeElement.scrollTop = 450;
    const scroll = vi.spyOn(window, 'scrollTo');
    c.goToPage(2);
    f.detectChanges();
    expect(c.results.nativeElement.scrollTop).toBe(0);
    expect(c.selected()).toBeNull();
    c.useStation();
    expect(chosen).not.toHaveBeenCalled();
    respond(2);
    f.detectChanges();
    expect(c.opened()).toBe(true);
    expect(scroll).not.toHaveBeenCalled();
    const use = [...f.nativeElement.querySelectorAll('button')].find(
      (b: any) => b.textContent.trim() === 'Use station',
    );
    expect(use.disabled).toBe(true);
    c.selected.set(c.stations()[0]);
    c.useStation();
    expect(chosen).toHaveBeenCalledWith(rows(2)[0]);
  });
  it('bounds page navigation and handles a visited page disappearing from the live catalogue', async () => {
    const f = await picker(rows(1)),
      c = f.componentInstance;
    c.goToPage(99);
    c.goToPage(0);
    http.expectNone((r) => r.url === root + '/stations/search');
    c.goToPage(2);
    respond(2);
    c.goToPage(3);
    respond(3);
    c.goToPage(2);
    respond(2, []);
    respond(1, [station]);
    expect(c.page()).toBe(1);
    expect(c.pages()).toEqual([1]);
    expect(c.stations()).toEqual([station]);
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
    expect(f.nativeElement.textContent).toContain('Browse RadioBrowser (www.radio-browser.info)');
    c.form.controls.type.setValue('UDP');
    c.changeType();
    f.detectChanges();
    expect(f.nativeElement.textContent).not.toContain(
      'Browse RadioBrowser (www.radio-browser.info)',
    );
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
  it('paging never changes the Source draft or emits a save/test request', async () => {
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
    c.radioPicker!.goToPage(2);
    http
      .expectOne((r) => r.url === root + '/stations/search')
      .flush({ stations: [station], offset: 20, limit: 20 });
    f.detectChanges();
    expect(c.draft()).toEqual(before);
    expect(c.dirty()).toBe(false);
    expect(c.radioPicker!.selected()).toBeNull();
    http.expectNone((r) => r.method !== 'GET');
    c.radioPicker!.close();
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
