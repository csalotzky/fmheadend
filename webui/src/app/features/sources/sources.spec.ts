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
  afterEach(() => {
    for (const request of http.match('/api/v1/alsa/capture-devices'))
      request.flush({ devices: [] });
    http.verify();
  });
  it('does not present an expired published Source observation as current', () => {
    const f = TestBed.createComponent(SourcesList);
    f.detectChanges();
    for (const request of http.match((r) => r.url.endsWith('/status'))) {
      request.flush({ source_id: 1, state: 'RUNNING', last_error: null, observation_stale: true });
    }
    f.detectChanges();
    expect(Object.values(f.componentInstance.runtime()).every((value) => value === null)).toBe(
      true,
    );
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
  it('selects explicit profiles or dynamic system default with independent gain', async () => {
    const f = editor(),
      c = f.componentInstance;
    await f.whenStable();
    fill(c);
    c.profiles.set([
      {
        name: 'Default',
        description: 'Profile details',
        path: 'default/basic.json',
        kind: 'default',
        is_system_default: true,
      },
      {
        name: 'Custom',
        description: 'Profile details',
        path: 'custom/server_123.json',
        kind: 'custom',
      },
    ]);
    c.form.controls.audio_profile_id.setValue('custom/server_123.json');
    c.form.controls.input_gain_db.setValue(-6);
    f.detectChanges();
    expect(
      f.nativeElement.querySelector('[formControlName="audio_profile_id"]').textContent,
    ).toContain('Custom');
    expect(c.draft()).toMatchObject({
      use_default_audio_profile: false,
      input_gain_db: -6,
      audio_profile_id: 'custom/server_123.json',
    });
    c.form.controls.audio_profile_id.setValue(null);
    f.detectChanges();
    expect(c.draft()).toMatchObject({
      use_default_audio_profile: true,
      input_gain_db: -6,
      audio_profile_id: null,
    });
    expect(f.nativeElement.querySelector('[formControlName="audio_profile_id"]').textContent).toContain('System default (Default)');
    expect(f.nativeElement.querySelector('[formControlName="input_gain_db"]').disabled).toBe(false);
  });
  it('groups Audio Profile options default first then custom, sorted, with display-only labels', async () => {
    const f = editor(),
      c = f.componentInstance;
    await f.whenStable();
    fill(c);
    c.profiles.set([
      {
        name: 'Teszt 1',
        description: 'Profile details',
        path: 'custom/teszt_1.json',
        kind: 'custom',
      },
      {
        name: 'moderate 3 dBr',
        description: 'Profile details',
        path: 'default/m3.json',
        kind: 'default',
      },
      {
        name: 'Basic',
        description: 'Profile details',
        path: 'default/basic.json',
        kind: 'default',
      },
      { name: 'Teszt', description: 'Profile details', path: 'custom/teszt.json', kind: 'custom' },
      {
        name: 'Aardvark',
        description: 'Profile details',
        path: 'custom/aardvark.json',
        kind: 'custom',
      },
      { name: 'Zeta', description: 'Profile details', path: 'default/zeta.json', kind: 'default' },
    ]);
    f.detectChanges();
    const select: HTMLSelectElement = f.nativeElement.querySelector(
      '[formControlName="audio_profile_id"]',
    );
    expect([...select.options].map((o) => o.textContent?.trim())).toEqual([
      'System default (unavailable)',
      '[default] Basic',
      '[default] moderate 3 dBr',
      '[default] Zeta',
      '[custom] Aardvark',
      '[custom] Teszt',
      '[custom] Teszt 1',
    ]);
    expect(c.form.controls.audio_profile_id.value).toBeNull();
    select.selectedIndex = 3;
    select.dispatchEvent(new Event('change'));
    expect(c.form.controls.audio_profile_id.value).toBe('default/zeta.json');
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('Profile details');
    expect(c.draft()).toMatchObject({ audio_profile_id: 'default/zeta.json' });
    expect(c.profiles().map((p) => p.name)).toContain('Zeta');
    expect(c.profiles().every((p) => !p.name.startsWith('['))).toBe(true);
  });
  it('rejects missing explicit profile without I/O', async () => {
    const f = editor(),
      c = f.componentInstance;
    await f.whenStable();
    fill(c);
    c.form.controls.audio_profile_id.setValue('custom/missing.json');
    expect(c.draft()).toBeNull();
    expect(f.nativeElement.querySelector('[formControlName="processor_mode"]')).toBeNull();
    http.expectNone('/api/v1/sources');
  });
  it('uses the shared centered editor with bottom Save and Cancel', () => {
    const f = editor();
    const el = f.nativeElement as HTMLElement;
    expect(el.classList.contains('page-content')).toBe(true);
    expect(el.classList.contains('editor-page')).toBe(true);
    expect(el.querySelector('.page-header h1')?.textContent).toBe('Add Source');
    expect(el.textContent).not.toContain('Back to Sources');
    expect(
      Array.from(el.querySelectorAll('.editor-actions button')).map((b) => b.textContent?.trim()),
    ).toEqual(['Save', 'Cancel']);
    f.componentInstance.form.markAsDirty();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    expect(f.componentInstance.canLeave()).toBe(false);
  });
  it('separates Source, Audio processing and RDS panels with actions below all', () => {
    const f = editor(),
      c = f.componentInstance;
    c.form.controls.type.setValue('HTTP');
    c.changeType();
    f.detectChanges();
    const panels = f.nativeElement.querySelectorAll('.editor-panel');
    expect(panels).toHaveLength(3);
    const source = panels[0] as HTMLElement;
    const audio = panels[1] as HTMLElement;
    const rds = panels[2] as HTMLElement;
    expect(source.parentElement).toBe(audio.parentElement);
    expect(audio.parentElement).toBe(rds.parentElement);
    expect(audio.nextElementSibling).toBe(rds);
    expect(rds.querySelector('h2')?.textContent).toBe('RDS');
    expect(source.querySelector('h2')?.textContent).toContain('Source');
    expect(audio.querySelector('h2')?.textContent).toContain('Audio processing');
    expect(source.querySelector('[formControlName="name"]')).toBeTruthy();
    expect(source.querySelector('[formControlName="type"]')).toBeTruthy();
    expect(source.querySelector('.stream-row')).toBeTruthy();
    expect(source.querySelector('details')).toBeNull();
    expect(audio.querySelector('[formControlName="audio_profile_id"]')).toBeTruthy();
    expect(audio.querySelector('[formControlName="input_gain_db"]')).toBeTruthy();
    expect(audio.querySelector('.stream-row')).toBeNull();
    expect(audio.querySelector('details')?.textContent).toContain('Advanced source options');
    f.detectChanges();
    expect(audio.querySelector('[formControlName="audio_profile_id"]')).toBeTruthy();
    const actions = f.nativeElement.querySelector('.editor-actions') as HTMLElement;
    expect(source.contains(actions)).toBe(false);
    expect(audio.contains(actions)).toBe(false);
    expect(rds.contains(actions)).toBe(false);
    expect(actions.textContent).not.toContain(
      'Testing does not save changes or prove RF transmission.',
    );
  });
  it.each<SourceType>(['HTTP', 'UDP', 'RTP', 'ALSA', 'LOCAL'])('renders only %s fields', (type) => {
    const f = editor(),
      c = f.componentInstance;
    c.form.controls.type.setValue(type);
    c.changeType();
    expect(f.nativeElement.querySelector('[formControlName="enabled"]')).toBeNull();
    f.detectChanges();
    const el = f.nativeElement as HTMLElement;
    const labels = [...el.querySelectorAll('label')].map((label) => label.textContent);
    expect(labels.some((label) => label?.includes('Stream URL'))).toBe(type === 'HTTP');
    expect(labels.some((label) => label?.includes('ALSA capture device'))).toBe(type === 'ALSA');
    expect(labels.some((label) => label?.includes('Port'))).toBe(['UDP', 'RTP'].includes(type));
    expect(el.querySelector('.editor-actions')?.textContent).not.toContain('Test');
    expect(el.querySelector('.editor-panel app-source-test-result')).toBeTruthy();
    if (type !== 'HTTP') {
      expect(el.querySelector('.source-test button')?.textContent?.trim()).toBe('Test source');
    }
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
    ['ALSA', { device: 'plughw:2,0' }, { device: 'plughw:2,0', channel_selection: 'stereo' }],
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
    expect(
      [...el.querySelectorAll('.editor-actions button')].some((action) =>
        action.textContent?.includes('Test'),
      ),
    ).toBe(false);
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
    expect(row.nextElementSibling?.tagName).toBe('APP-SOURCE-TEST-RESULT');
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
      'LOCAL',
      'PODCAST',
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
    expect(c.draft()?.config).toEqual({ device: 'hw:1,0', channel_selection: 'stereo' });
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
    expect(c.message()).toBe('');
    expect(c.dirty()).toBe(false);
    expect(c.form.controls.name.value).toBe('FM4');
    expect(f.nativeElement.querySelector('.editor-feedback').textContent).toContain(
      'Source saved, but audio restart failed',
    );
    expect(navigate).not.toHaveBeenCalled();
    expect(shared.refreshAfterMutation).toHaveBeenCalledOnce();
    const stopped = sample();
    stopped.status.system.snapshot_stale = true;
    Object.assign(stopped.status.modulators[0], { tx_state: 'OFF', source_last_error: null });
    shared.data.set(stopped);
    f.detectChanges();
    expect(f.nativeElement.querySelector('.editor-feedback [role="alert"]')).not.toBeNull();
    stopped.status.system.snapshot_stale = false;
    shared.data.set({ ...stopped });
    f.detectChanges();
    expect(f.nativeElement.querySelector('.editor-feedback [role="alert"]')).toBeNull();
    shared.data.set(sample());
    f.detectChanges();
    expect(f.nativeElement.querySelector('.editor-feedback [role="alert"]')).toBeNull();
  });
  it('shows accepted application without treating RUNNING as confirmation of the saved revision', async () => {
    Object.assign(TestBed.inject(ActivatedRoute).snapshot, {
      paramMap: convertToParamMap({ id: '1' }),
    });
    const f = editor(), c = f.componentInstance;
    http.expectOne('/api/v1/sources/1').flush(sample().sources[0]);
    await f.whenStable();
    fill(c);
    const saving = c.save();
    const request = http.expectOne('/api/v1/sources/1');
    const timestamp = '2026-11-22T10:00:00Z';
    request.flush({
      ...sample().sources[0], ...request.request.body, timestamp, saved: true, applied: false,
      apply_pending: 'Waiting for the previous audio worker to stop.',
      apply_error: null,
    });
    await saving;
    const old = sample();
    old.status.modulators[0].source_state = 'RUNNING';
    shared.data.set(old);
    f.detectChanges();
    const feedback = () => f.nativeElement.querySelector('.editor-feedback') as HTMLElement;
    expect(feedback().textContent).toContain('Waiting for the previous audio worker');
    expect(feedback().querySelector('[role="alert"]')).toBeNull();
    const current = sample();
    current.sources[0].timestamp = timestamp;
    current.status.modulators[0].source_state = 'RUNNING';
    shared.data.set(current);
    f.detectChanges();
    expect(feedback().textContent).toContain('Audio application accepted');
    expect(feedback().textContent).not.toContain('Waiting for');
    expect(feedback().textContent).not.toContain('applied');
    const newer = sample();
    newer.sources[0].timestamp = '2026-11-22T10:00:01Z';
    shared.data.set(newer);
    f.detectChanges();
    expect(feedback().textContent).not.toContain('Audio application accepted');
  });
  it('stays after creation and updates the returned ID on the next save', async () => {
    const f = editor(),
      c = f.componentInstance;
    fill(c);
    const nav = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const p = c.save();
    const req = http.expectOne('/api/v1/sources');
    expect(req.request.method).toBe('POST');
    expect(req.request.body.config).toEqual({ uri: 'https://example.org/live' });
    expect(nav).not.toHaveBeenCalled();
    req.flush(sample().sources[0]);
    await p;
    expect(nav).not.toHaveBeenCalled();
    expect(c.id).toBe(1);
    expect(c.dirty()).toBe(false);
    expect(c.canLeave()).toBe(true);
    f.detectChanges();
    expect(f.nativeElement.querySelector('.editor-feedback').textContent).toContain(
      'Source saved.',
    );
    c.form.controls.name.setValue('Renamed');
    c.form.markAsDirty();
    const again = c.save();
    const update = http.expectOne('/api/v1/sources/1');
    expect(update.request.method).toBe('PATCH');
    update.flush({ ...sample().sources[0], ...update.request.body, saved: true, applied: true });
    await again;
    expect(c.form.controls.name.value).toBe('Renamed');
    expect(c.dirty()).toBe(false);
    expect(nav).not.toHaveBeenCalled();
    c.form.controls.name.setValue('');
    c.form.markAsDirty();
    await c.save();
    expect(c.message()).toBe('');
    expect(c.error()).not.toBe('');
    expect(c.dirty()).toBe(true);
    http.expectNone('/api/v1/sources/1');
    expect(shared.refreshAfterMutation).toHaveBeenCalled();
  });
  it('preserves form on save failure and prevents duplicate saves', async () => {
    const nav = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
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
    expect(c.message()).toBe('');
    expect(c.id).toBeUndefined();
    expect(nav).not.toHaveBeenCalled();
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
  it('shows testing and transport errors beside the source and invalidates them on edits', async () => {
    const f = editor(),
      c = f.componentInstance;
    fill(c);
    const p = c.test();
    f.detectChanges();
    expect(
      f.nativeElement.querySelector('.http-url-field app-source-test-result').textContent,
    ).toContain('Testing...');
    http
      .expectOne('/api/v1/sources/test')
      .flush({ detail: 'Probe unavailable' }, { status: 503, statusText: 'Unavailable' });
    await p;
    f.detectChanges();
    expect(f.nativeElement.querySelector('.http-url-field [role="alert"]')).toBeTruthy();
    expect(c.error()).toBe('');
    c.config.controls['uri'].setValue('https://changed.example/live');
    expect(c.testError()).toBe('');
  });
  it('invalidates audio results on processing changes and discards outdated replies', async () => {
    const c = editor().componentInstance;
    fill(c);
    c.result.set(result);
    c.form.controls.input_gain_db.setValue(-3);
    expect(c.result()).toBeNull();
    const p = c.test();
    c.config.controls['uri'].setValue('https://changed.example/live');
    http.expectOne('/api/v1/sources/test').flush(result);
    await p;
    expect(c.result()).toBeNull();
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
  it('marks each source card with its type for the colour indicator, not its state', () => {
    const data = sample();
    const types = ['HTTP', 'UDP', 'RTP', 'ALSA', 'LOCAL', 'PODCAST'] as const;
    data.sources = types.map((type, i) => ({ ...data.sources[0], id: i + 1, type }));
    shared.data.set(data);
    const f = list();
    for (const i of types.keys())
      http.expectOne(`/api/v1/sources/${i + 1}/status`).flush({ state: 'RUNNING' });
    f.detectChanges();
    const rows = [...f.nativeElement.querySelectorAll('.source-row')] as HTMLElement[];
    expect(rows.map((row) => row.dataset['sourceType'])).toEqual(['HTTP', 'LOCAL', 'PODCAST', 'UDP', 'RTP', 'ALSA']);
    expect(f.nativeElement.querySelectorAll('details.source-group')).toHaveLength(6);
    expect(rows.every((row) => row.classList.contains('list-card'))).toBe(true);
    for (const label of ['Internet stream (HTTP)', 'Network stream (UDP)',
      'Network stream (RTP)', 'Sound card (ALSA)', 'Local media', 'Podcast'])
      expect(f.nativeElement.textContent).toContain(label);
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
  it.each(['HTTP', 'LOCAL', 'ALSA', 'UDP', 'RTP'] as const)('tests saved %s by id with duplicate prevention', async (type) => {
    const data = sample();
    data.sources[0].type = type;
    shared.data.set(data);
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

  function savedPodcastList() {
    const data = sample();
    const source = {
      ...data.sources[0], type: 'PODCAST' as const, input_gain_db: -3,
      use_default_audio_profile: false, audio_profile_id: 'custom/talk.json',
      gst_options: { queue: { silent: true } }, rds_profile: null,
      config: {
        schedule_mode: 'RANDOM', refresh_interval_minutes: 17, replay_cooldown_hours: 4,
        feeds: [
          { url: 'https://first.example/rss', max_age_days: 10, max_episodes: null },
          { url: 'https://second.example/rss', max_age_days: null, max_episodes: 0 },
          { url: 'https://third.example/rss', max_age_days: 2, max_episodes: 4 },
        ],
      },
    };
    data.sources = [source, { ...data.sources[0], id: 2 }];
    shared.data.set(data);
    const f = list();
    for (const req of http.match((r) => r.url.endsWith('/status'))) req.flush({ state: 'IDLE' });
    const row = f.nativeElement.querySelector('[data-source-type="PODCAST"]') as HTMLElement;
    return { source, f, row, c: f.componentInstance };
  }

  function oneFeedProbe(title: string, discovered: number, eligible: number, error: string | null = null) {
    return {
      ...result, ok: eligible > 0,
      podcast: {
        feeds_attempted: 1, feeds_succeeded: error ? 0 : 1,
        discovered_episodes: discovered, eligible_episodes: eligible,
        feeds: [{ feed_id: title, title, error, discovered_episodes: discovered, eligible_episodes: error ? null : eligible }],
      },
    };
  }

  it('tests the saved feed settings sequentially and displays each result before the final summary', async () => {
    const { f, c, source, row } = savedPodcastList();
    const original = structuredClone(source);
    const done = c.test(1);
    const replies = [
      oneFeedProbe('Offline', 0, 0, 'PODCAST_FETCH_TIMEOUT'),
      oneFeedProbe('Filtered', 14, 0),
      oneFeedProbe('Playable', 9, 4),
    ];
    for (const [index, feed] of source.config.feeds.entries()) {
      f.detectChanges();
      expect(row.querySelector('progress')?.value).toBe(index);
      expect(row.textContent).toContain(`${index} / 3 feeds tested`);
      expect(row.textContent).toContain(feed.url);
      expect(row.querySelectorAll('.podcast-feeds li')).toHaveLength(index);
      expect(row.querySelector('.podcast-summary')).toBeNull();
      await c.test(1);
      await c.test(2);
      const req = http.expectOne('/api/v1/sources/test');
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({
        name: source.name, type: source.type, input_gain_db: -3,
        use_default_audio_profile: false, audio_profile_id: source.audio_profile_id,
        gst_options: source.gst_options, rds_profile: null, config: { ...source.config, feeds: [feed] },
      });
      http.expectNone('/api/v1/sources/2/test');
      req.flush(replies[index]);
      await Promise.resolve();
    }
    await done;
    f.detectChanges();
    expect(source).toEqual(original);
    expect(row.textContent).toContain('3 / 3 feeds tested');
    expect(row.textContent).toContain('1 of 3 feeds failed');
    expect(row.textContent).toContain('Timed out');
    expect(row.textContent).toContain('OK: 14 discovered / 0 eligible');
    expect([...row.querySelectorAll('.podcast-summary dd')].map(dd => dd.textContent?.trim())).toEqual([
      '2 / 3', '23 discovered', '4',
    ]);
    expect(c.results()[1]?.ok).toBe(true);
  });

  it.each(['DISCOVERY_BUSY', 'DISCOVERY_TIMEOUT', 'client timeout'])('retains completed list diagnostics on %s and resets on retry', async (reason) => {
    const { f, c, row } = savedPodcastList();
    vi.useFakeTimers();
    try {
      const done = c.test(1);
      http.expectOne('/api/v1/sources/test').flush(oneFeedProbe('Completed', 3, 0));
      await Promise.resolve();
      const interrupted = http.expectOne('/api/v1/sources/test');
      if (reason === 'client timeout') await vi.advanceTimersByTimeAsync(205_000);
      else interrupted.flush({ error: reason, message: 'Request interrupted' }, { status: 503, statusText: 'Unavailable' });
      await done;
      f.detectChanges();
      http.expectNone('/api/v1/sources/test');
      expect(row.textContent).toContain(reason === 'client timeout' ? 'timed out after 205 seconds' : reason);
      expect(row.textContent).toContain('Test stopped');
      expect(row.textContent).toContain('1 / 3 feeds tested');
      expect(row.textContent).toContain('Completed');
      expect(row.querySelectorAll('.podcast-feeds li')).toHaveLength(1);
      expect(row.querySelector('.podcast-summary')).toBeNull();
      const retry = c.test(1);
      f.detectChanges();
      expect(row.textContent).toContain('0 / 3 feeds tested');
      expect(row.querySelectorAll('.podcast-feeds li')).toHaveLength(0);
      const pending = http.expectOne('/api/v1/sources/test');
      f.destroy();
      await retry;
      expect(pending.cancelled).toBe(true);
      http.expectNone('/api/v1/sources/test');
    } finally {
      vi.useRealTimers();
    }
  });

  it('discards a delivered response when the list is destroyed before its continuation', async () => {
    const { f, c } = savedPodcastList();
    const done = c.test(1);
    http.expectOne('/api/v1/sources/test').flush(oneFeedProbe('Stale', 2, 1));
    f.destroy();
    await done;
    expect(c.results()[1]).toBeNull();
    expect(shared.refresh).not.toHaveBeenCalled();
    http.expectNone('/api/v1/sources/test');
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
  it('shows the scheduler conflict when Source deletion is blocked', async () => {
    const f = list();
    http.expectOne('/api/v1/sources/1/status').flush({ state: 'IDLE' });
    const message = 'Cannot delete source: it is used by a scheduler rule.';
    f.componentInstance.deleting.set(1);
    const removal = f.componentInstance.remove(1);
    http.expectOne('/api/v1/sources/1').flush(
      { error: 'RESOURCE_IN_USE', message },
      { status: 409, statusText: 'Conflict' },
    );
    await removal;
    f.detectChanges();
    expect(f.nativeElement.querySelector('[role="alert"]').textContent.trim()).toBe(message);
    expect(shared.refreshAfterMutation).not.toHaveBeenCalled();
  });
  it('shows the stored LOCAL path instead of a network endpoint', () => {
    expect(
      endpoint({
        ...sample().sources[0],
        type: 'LOCAL',
        config: { path: '/mnt/music-link//album' },
      }),
    ).toBe('/mnt/music-link//album');
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
  it('resolves Audio profile from shared names, falling back without extra requests', () => {
    const data = sample();
    data.sources = [
      {
        ...data.sources[0],
        id: 1,
        use_default_audio_profile: false,
        audio_profile_id: 'custom/studio.json',
      },
      {
        ...data.sources[0],
        id: 2,
        use_default_audio_profile: false,
        audio_profile_id: 'custom/missing.json',
      },
      { ...data.sources[0], id: 3, use_default_audio_profile: true },
      { ...data.sources[0], id: 4, use_default_audio_profile: false, audio_profile_id: 'default/bypass.json' },
    ];
    shared.data.set(data);
    shared.profileNames.set({ 'custom/studio.json': 'Studio processing', 'default/basic_filtering.json': 'Basic Filtering', 'default/bypass.json': 'Bypass' });
    shared.defaultAudioProfileId.set('default/basic_filtering.json');
    const f = TestBed.createComponent(SourcesList);
    f.detectChanges();
    for (const request of http.match((r) => r.url.endsWith('/status')))
      request.flush({ state: 'IDLE' });
    const labels = () =>
      Array.from(f.nativeElement.querySelectorAll('.audio-profile') as NodeListOf<HTMLElement>).map(
        (e) => e.textContent?.trim(),
      );
    expect(labels()).toEqual([
      'Audio: Studio processing',
      'Audio: custom/missing.json',
      'Audio: System default (Basic Filtering)',
      'Audio: Bypass',
    ]);
    shared.profileNames.set({});
    f.detectChanges();
    expect(labels()[0]).toBe('Audio: custom/studio.json');
    http.expectNone('/api/v1/audio-profiles/files');
    f.destroy();
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
      use_default_audio_profile: false,
      audio_profile_id: 'custom/studio.json',
      gst_options: { queue: { 'max-size-buffers': 4 } },
    });
    await f.whenStable();
    const c = f.componentInstance;
    c.form.controls.name.setValue('Renamed');
    const p = c.save();
    const req = http.expectOne('/api/v1/sources/1');
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body.audio_profile_id).toBe('custom/studio.json');
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
    expect(text).toContain('Channels2');
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
