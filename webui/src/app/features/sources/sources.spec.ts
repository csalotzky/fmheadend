import { TestBed } from '@angular/core/testing';
import { provideRouter, ActivatedRoute, convertToParamMap, Router } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { TestResult } from './test-result';
import { SourceEditor } from './source-editor';
import { SourcesList, endpoint } from './sources-list';
import { SourceApi, SourceType } from './source-api';
import { DashboardState } from '../dashboard/dashboard-state';
import { sample } from '../dashboard/test-fixtures';
const result = {
  ok: true,
  mocked: false,
  codec: 'audio/mpeg',
  sample_rate: 44100,
  channels: 2,
  output_caps: 'audio/x-raw',
  stream_title: 'Test',
  message: 'Connected',
};
describe('Sources management', () => {
  let http: HttpTestingController;
  let shared: DashboardState;
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
    shared = TestBed.inject(DashboardState);
    shared.data.set(sample());
    shared.stale.set(false);
    shared.loading.set(false);
    vi.spyOn(shared, 'refreshAfterMutation').mockResolvedValue();
    vi.spyOn(shared, 'refresh').mockResolvedValue();
  });
  afterEach(() => http.verify());
  it('does not present an expired published Source observation as current', () => {
    const f = TestBed.createComponent(SourcesList);
    f.detectChanges();
    for (const request of http.match((r) => r.url.endsWith('/status'))) {
      request.flush({ source_id: 1, state: 'RUNNING', last_error: null, observation_stale: true });
    }
    f.detectChanges();
    expect(Object.values(f.componentInstance.runtime()).every((value) => value === null)).toBe(true);
    f.destroy();
  });
  function editor(id?: number) {
    if (id !== undefined)
      TestBed.overrideProvider(ActivatedRoute, {
        useValue: { snapshot: { paramMap: convertToParamMap({ id: String(id) }) } },
      });
    const f = TestBed.createComponent(SourceEditor);
    f.detectChanges();
    http.expectOne('/api/v1/audio-profiles/files').flush([]);
    return f;
  }
  function fill(c: SourceEditor) {
    c.form.controls.name.setValue('FM4');
    c.config.controls['uri'].setValue('https://example.org/live');
  }
  it('selects file paths with independent gain and clears profile for Bypass', async () => {
    const f = editor(),
      c = f.componentInstance;
    await f.whenStable();
    fill(c);
    c.profiles.set([
      { name: 'Default', path: 'default/basic.json', kind: 'default' },
      { name: 'Custom', path: 'custom/server_123.json', kind: 'custom' },
    ]);
    c.form.controls.processor_mode.setValue('builtin');
    c.changeProcessor();
    c.form.controls.audio_profile_path.setValue('custom/server_123.json');
    c.form.controls.input_gain_db.setValue(-6);
    f.detectChanges();
    expect(
      f.nativeElement.querySelector('[formControlName="audio_profile_path"]').textContent,
    ).toContain('Custom');
    expect(c.draft()).toMatchObject({
      processor_mode: 'builtin',
      input_gain_db: -6,
      audio_profile_path: 'custom/server_123.json',
    });
    c.form.controls.processor_mode.setValue('bypass');
    c.changeProcessor();
    f.detectChanges();
    expect(c.draft()).toMatchObject({
      processor_mode: 'bypass',
      input_gain_db: -6,
      audio_profile_path: null,
    });
    expect(f.nativeElement.querySelector('[formControlName="audio_profile_path"]')).toBeNull();
    expect(f.nativeElement.querySelector('[formControlName="input_gain_db"]').disabled).toBe(false);
  });
  it('rejects missing Built-in profile and unavailable Stereo Tool without I/O', async () => {
    const f = editor(),
      c = f.componentInstance;
    await f.whenStable();
    fill(c);
    c.form.controls.processor_mode.setValue('builtin');
    expect(c.draft()).toBeNull();
    c.form.controls.audio_profile_path.setValue('custom/missing.json');
    expect(c.draft()).toBeNull();
    c.form.controls.processor_mode.setValue('stereotool');
    expect(c.draft()).toBeNull();
    expect(f.nativeElement.querySelector('option[value="stereotool"]').disabled).toBe(true);
    http.expectNone('/api/v1/sources');
  });
  it('uses the shared centered editor with bottom Save and Cancel', () => {
    const f = editor();
    const el = f.nativeElement as HTMLElement;
    expect(el.classList.contains('page-content')).toBe(true);
    expect(el.classList.contains('editor-narrow')).toBe(true);
    expect(el.textContent).not.toContain('Back to Sources');
    expect(
      Array.from(el.querySelectorAll('.editor-actions button')).map((b) => b.textContent?.trim()),
    ).toEqual(['Save', 'Cancel']);
    f.componentInstance.form.markAsDirty();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    expect(f.componentInstance.canLeave()).toBe(false);
  });
  it.each<SourceType>(['HTTP', 'UDP', 'RTP', 'ALSA'])('renders only %s fields', (type) => {
    const f = editor(),
      c = f.componentInstance;
    c.form.controls.type.setValue(type);
    c.changeType();
    expect(f.nativeElement.querySelector('[formControlName="enabled"]')).toBeNull();
    f.detectChanges();
    const el = f.nativeElement as HTMLElement;
    const labels = [...el.querySelectorAll('label')].map((label) => label.textContent);
    expect(labels.some((label) => label?.includes('Stream URL'))).toBe(type === 'HTTP');
    expect(labels.some((label) => label?.includes('ALSA device'))).toBe(type === 'ALSA');
    expect(labels.some((label) => label?.includes('Port'))).toBe(['UDP', 'RTP'].includes(type));
  });
  it.each([
    [
      'UDP',
      { port: '5000', format: 'S16LE', sample_rate: '48000', channels: '2' },
      { port: 5000, format: 'S16LE', sample_rate: 48000, channels: 2 },
    ],
    [
      'RTP',
      { port: '5004', codec: 'OPUS', payload_type: '96' },
      { port: 5004, codec: 'OPUS', payload_type: 96 },
    ],
    ['ALSA', { device: 'plughw:2,0' }, { device: 'plughw:2,0' }],
  ])('serializes %s without invented defaults', (type, values, expected) => {
    const c = editor().componentInstance;
    c.form.controls.name.setValue('Input');
    c.form.controls.type.setValue(type as SourceType);
    c.changeType();
    for (const [key, value] of Object.entries(values))
      c.config.controls[key].setValue(String(value));
    expect(c.draft()?.config).toEqual(expected);
  });
  it('tests the unsaved HTTP stream from its URL row without saving', async () => {
    const f = editor(),
      c = f.componentInstance;
    fill(c);
    f.detectChanges();
    const el = f.nativeElement as HTMLElement;
    const row = el.querySelector('.stream-row')!;
    expect(row.querySelectorAll('input')).toHaveLength(1);
    const button = row.querySelector('button')!;
    expect(button.textContent?.trim()).toBe('Test stream');
    expect(button.type).toBe('button');
    expect(el.querySelector('.editor-actions')?.textContent).not.toContain('Test');
    button.click();
    f.detectChanges();
    expect(button.disabled).toBe(true);
    const request = http.expectOne('/api/v1/sources/test');
    expect(request.request.body.config.uri).toBe('https://example.org/live');
    http.expectNone('/api/v1/sources');
    request.flush(result);
    await f.whenStable();
    f.detectChanges();
    expect(el.querySelector('app-source-test-result')?.textContent).toContain('Test successful');
    expect(button.disabled).toBe(false);
  });
  it('has no duplicate User agent field and preserves the advanced property', () => {
    const f = editor(),
      c = f.componentInstance;
    fill(c);
    expect((f.nativeElement as HTMLElement).textContent).not.toContain('User agent');
    expect(c.config.controls['user_agent']).toBeUndefined();
    c.form.controls.gst_options.setValue('{"source":{"user-agent":"Station/2"}}');
    expect(c.draft()?.gst_options['source']['user-agent']).toBe('Station/2');
  });
  it.each(['', '   ', 'My deliberate name'])(
    'autofills only a blank HTTP name (%s)',
    async (name) => {
      const f = editor(),
        c = f.componentInstance;
      fill(c);
      c.form.controls.name.setValue(name);
      const promise = c.test();
      http.expectOne('/api/v1/sources/test').flush({ ...result, station_name: 'Station FM' });
      await promise;
      f.detectChanges();
      expect(c.form.controls.name.value).toBe(name.trim() ? name : 'Station FM');
      expect(f.nativeElement.textContent).toContain('Station: Station FM');
      expect(f.nativeElement.textContent).toContain('44100 Hz');
      expect(f.nativeElement.textContent).toContain('audio/x-raw');
      http.expectNone('/api/v1/sources');
    },
  );
  it('leaves a blank name alone without reliable station identity', async () => {
    const c = editor().componentInstance;
    fill(c);
    c.form.controls.name.setValue('');
    const promise = c.test();
    http.expectOne('/api/v1/sources/test').flush({ ...result, station_name: null });
    await promise;
    expect(c.form.controls.name.value).toBe('');
    expect(c.draft()).toBeNull();
  });
  it('offers only digital InputSource types', () => {
    const f = editor();
    const select = (f.nativeElement as HTMLElement).querySelector(
      'select[formcontrolname="type"]',
    ) as HTMLSelectElement;
    expect([...select.options].map((option) => option.value)).toEqual([
      'HTTP',
      'UDP',
      'RTP',
      'ALSA',
    ]);
  });
  it('accepts actual numeric input events', () => {
    const f = editor(),
      c = f.componentInstance;
    fill(c);
    const input = (f.nativeElement as HTMLElement).querySelector(
      'input[type="number"]',
    ) as HTMLInputElement;
    input.value = '2500';
    input.dispatchEvent(new Event('input'));
    expect(c.draft()?.config['buffer_ms']).toBe(2500);
  });
  it('clears incompatible config and advanced options on type switch', () => {
    const c = editor().componentInstance;
    fill(c);
    c.form.controls.gst_options.setValue('{"source":{"x":1}}');
    c.form.controls.type.setValue('ALSA');
    c.changeType();
    c.config.controls['device'].setValue('hw:1,0');
    expect(c.draft()?.config).toEqual({ device: 'hw:1,0' });
    expect(c.draft()?.gst_options).toEqual({});
  });
  it('saves an active source in one request and retains the saved form on apply failure', async () => {
    Object.assign(TestBed.inject(ActivatedRoute).snapshot, {
      paramMap: convertToParamMap({ id: '1' }),
    });
    const f = editor(),
      c = f.componentInstance;
    http.expectOne('/api/v1/sources/1').flush(sample().sources[0]);
    await f.whenStable();
    fill(c);
    c.form.markAsDirty();
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const pending = c.save();
    await c.save();
    const req = http.expectOne('/api/v1/sources/1');
    expect(req.request.method).toBe('PATCH');
    req.flush({
      ...sample().sources[0],
      ...req.request.body,
      saved: true,
      applied: false,
      apply_error: 'audio start failed',
    });
    await pending;
    f.detectChanges();
    expect(c.form.pristine).toBe(true);
    expect(c.config.pristine).toBe(true);
    expect(c.form.controls.name.value).toBe('FM4');
    expect(f.nativeElement.querySelector('.editor-feedback').textContent).toContain(
      'Source saved, but audio restart failed',
    );
    expect(navigate).not.toHaveBeenCalled();
    expect(shared.refreshAfterMutation).toHaveBeenCalledOnce();
  });
  it('creates HTTP source only after successful save', async () => {
    const c = editor().componentInstance;
    fill(c);
    const nav = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const p = c.save();
    const req = http.expectOne('/api/v1/sources');
    expect(req.request.method).toBe('POST');
    expect(req.request.body.config).toEqual({ uri: 'https://example.org/live' });
    expect(nav).not.toHaveBeenCalled();
    req.flush(sample().sources[0]);
    await p;
    expect(nav).toHaveBeenCalledWith(['/sources']);
    expect(shared.refreshAfterMutation).toHaveBeenCalled();
  });
  it('preserves form on save failure and prevents duplicate saves', async () => {
    const f = editor(),
      c = f.componentInstance;
    fill(c);
    c.form.markAsDirty();
    const p = c.save();
    await c.save();
    http
      .expectOne('/api/v1/sources')
      .flush({ message: 'Source active' }, { status: 409, statusText: 'Conflict' });
    await p;
    expect(c.form.controls.name.value).toBe('FM4');
    expect(c.dirty()).toBe(true);
    expect(c.error()).toBe('Source active');
    f.detectChanges();
    expect(f.nativeElement.querySelector('.editor-feedback').textContent).toContain(
      'Source active',
    );
    expect(f.nativeElement.querySelectorAll('[role="alert"]')).toHaveLength(1);
  });
  it('tests unsaved config without persistence and prevents duplicates', async () => {
    const c = editor().componentInstance;
    fill(c);
    const p = c.test();
    await c.test();
    const req = http.expectOne('/api/v1/sources/test');
    expect(req.request.body.name).toBe('FM4');
    req.flush(result);
    await p;
    expect(c.result()?.ok).toBe(true);
    expect(shared.refreshAfterMutation).not.toHaveBeenCalled();
  });
  it('shows failed probe results', async () => {
    const f = editor(),
      c = f.componentInstance;
    fill(c);
    const p = c.test();
    http
      .expectOne('/api/v1/sources/test')
      .flush({ ...result, ok: false, message: 'Connection refused' });
    await p;
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('Test failed');
    expect(f.nativeElement.textContent).toContain('Connection refused');
  });
  it('protects dirty navigation but permits pristine navigation', () => {
    const c = editor().componentInstance;
    expect(c.canLeave()).toBe(true);
    c.config.markAsDirty();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    expect(c.canLeave()).toBe(false);
    confirm.mockReturnValue(true);
    expect(c.canLeave()).toBe(true);
    confirm.mockRestore();
  });
  it('rejects invalid URL, numeric and advanced JSON input', () => {
    const c = editor().componentInstance;
    fill(c);
    c.config.controls['uri'].setValue('ftp://bad');
    expect(c.draft()).toBeNull();
    c.config.controls['uri'].setValue('http://example.org');
    c.config.controls['buffer_ms'].setValue('-1');
    expect(c.draft()).toBeNull();
    c.config.controls['buffer_ms'].setValue('');
    c.form.controls.gst_options.setValue('[]');
    expect(c.draft()).toBeNull();
  });
  function list() {
    const f = TestBed.createComponent(SourcesList);
    f.detectChanges();
    return f;
  }
  it.each(['IDLE', 'RUNNING', 'ERROR'])('renders source %s and eight references', (state) => {
    const f = list();
    http
      .expectOne('/api/v1/sources/1/status')
      .flush({ source_id: 1, state, last_error: state === 'ERROR' ? 'Disconnected' : null });
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain(state);
    expect(f.nativeElement.textContent).toContain('8 TX references');
    expect(f.nativeElement.textContent).toContain('Studio feed');
    expect(f.nativeElement.querySelector('a.button-link')?.getAttribute('href')).toBe('/sources/1');
  });
  it('shows unavailable runtime on failed status request', () => {
    const f = list();
    http.expectOne('/api/v1/sources/1/status').error(new ProgressEvent('error'));
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('UNKNOWN / Unavailable');
  });
  it('shows empty state', () => {
    const data = sample();
    data.sources = [];
    shared.data.set(data);
    expect(list().nativeElement.textContent).toContain('No Sources configured');
  });
  it('tests saved source with duplicate prevention', async () => {
    const f = list();
    http.expectOne('/api/v1/sources/1/status').flush({ state: 'IDLE' });
    const c = f.componentInstance;
    const p = c.test(1);
    await c.test(1);
    http
      .expectOne('/api/v1/sources/1/test')
      .flush({ ...result, station_name: 'Different station' });
    await p;
    expect(c.results()[1]?.ok).toBe(true);
    expect(shared.data()?.sources[0].name).toBe('Studio feed');
    http.expectNone('/api/v1/sources/1');
  });
  it.each([true, false])('handles delete success=%s without cascade', async (success) => {
    const c = list().componentInstance;
    http.expectOne('/api/v1/sources/1/status').flush({ state: 'IDLE' });
    await c.remove(1);
    http.expectNone('/api/v1/sources/1');
    c.deleting.set(1);
    const p = c.remove(1);
    const req = http.expectOne('/api/v1/sources/1');
    expect(req.request.method).toBe('DELETE');
    if (success) req.flush(null);
    else
      req.flush(
        { message: 'Resource is referenced by another configuration' },
        { status: 409, statusText: 'Conflict' },
      );
    await p;
    if (success) expect(shared.refreshAfterMutation).toHaveBeenCalled();
    else expect(c.errors()[1]).toContain('referenced');
  });
  it('redacts URL credentials and query from list summaries', () => {
    const source = sample().sources[0];
    source.config = { uri: 'https://user:secret@example.org/live?token=secret' };
    expect(endpoint(source)).toBe('https://example.org/live');
  });
  it('recovers runtime refresh through shared polling updates', () => {
    const f = list();
    http.expectOne('/api/v1/sources/1/status').error(new ProgressEvent('error'));
    shared.data.set(sample());
    f.detectChanges();
    http.expectOne('/api/v1/sources/1/status').flush({ state: 'RUNNING' });
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('RUNNING');
    shared.stale.set(true);
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('Backend unavailable');
  });
});
describe('Saved source editor', () => {
  it('loads and patches existing source, preserving optional properties', async () => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: convertToParamMap({ id: '1' }) } },
        },
      ],
    });
    const http = TestBed.inject(HttpTestingController);
    vi.spyOn(TestBed.inject(DashboardState), 'refreshAfterMutation').mockResolvedValue();
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const f = TestBed.createComponent(SourceEditor);
    f.detectChanges();
    http
      .expectOne('/api/v1/audio-profiles/files')
      .flush([{ path: 'custom/studio.json', name: 'Studio', kind: 'custom' }]);
    http.expectOne('/api/v1/sources/1').flush({
      ...sample().sources[0],
      processor_mode: 'builtin',
      audio_profile_path: 'custom/studio.json',
      gst_options: { queue: { 'max-size-buffers': 4 } },
    });
    await f.whenStable();
    const c = f.componentInstance;
    c.form.controls.name.setValue('Renamed');
    const p = c.save();
    const req = http.expectOne('/api/v1/sources/1');
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body.audio_profile_path).toBe('custom/studio.json');
    expect(req.request.body.gst_options.queue['max-size-buffers']).toBe(4);
    req.flush(sample().sources[0]);
    await p;
    http.verify();
  });
});

describe('Input bitrate result', () => {
  it.each([
    [192000, '192'],
    [191873, '192'],
    [128499, '128'],
  ])('rounds reported %s bps to approximate kb/s', (value, expected) => {
    const f = TestBed.createComponent(TestResult);
    f.componentRef.setInput('result', {
      ...result,
      input_bitrate_bps: value,
      station_name: 'Station FM',
    });
    f.detectChanges();
    const text = (f.nativeElement as HTMLElement).textContent;
    expect(text).toContain(`Bitrate: ~${expected} kb/s`);
    expect(text).toContain('Station: Station FM');
    expect(text).toContain('audio/mpeg');
    expect(text).toContain('44100 Hz');
    expect(text).toContain('2 channels');
    expect(text).toContain('audio/x-raw');
  });
  it.each([null, undefined, 0])('omits unavailable bitrate %s', (value) => {
    const f = TestBed.createComponent(TestResult);
    f.componentRef.setInput('result', { ...result, input_bitrate_bps: value });
    f.detectChanges();
    expect(f.nativeElement.textContent).not.toContain('Bitrate:');
  });
  it('does not display bitrate on a failed test', () => {
    const f = TestBed.createComponent(TestResult);
    f.componentRef.setInput('result', { ...result, ok: false, input_bitrate_bps: 192000 });
    f.detectChanges();
    expect(f.nativeElement.textContent).not.toContain('Bitrate:');
  });
});
