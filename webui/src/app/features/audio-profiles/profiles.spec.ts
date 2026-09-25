import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { ProfilesList } from './profiles-list';
import { ProfileEditor } from './profile-editor';
import { AudioProfile, ProfileResult, DspDescription } from './profile-api';
const base = '/api/v1/audio-profiles/files';
const description: DspDescription = {
  template: { schema_version: 1, name: '', stages: {} },
  stages: [
    { key: 'input_gain', label: 'Profile Input Gain', choices: ['VOLUME'] },
    { key: 'hpf', label: 'HPF', choices: ['OFF', 'CHEBYSHEV'] },
    { key: 'gate', label: 'Gate / Expander', choices: ['OFF', 'LSP_EXPANDER', 'LSP_GATE'] },
    { key: 'agc', label: 'AGC / Leveller', choices: ['OFF', 'LSP_AGC'] },
    { key: 'eq', label: 'EQ', choices: ['OFF', 'LSP_EQ'] },
    { key: 'wideband', label: 'Wideband Compressor', choices: ['OFF', 'LSP_COMPRESSOR'] },
    { key: 'multiband', label: 'Multiband Compressor', choices: ['OFF', 'LSP_MULTIBAND'] },
    { key: 'master_gain', label: 'Master Gain', choices: ['VOLUME'] },
    { key: 'limiter', label: 'Limiter', choices: ['OFF', 'LSP_LIMITER'] },
    { key: 'lpf', label: 'LPF', choices: ['OFF', 'CHEBYSHEV'] },
    { key: 'output_trim', label: 'Output Trim', choices: ['TRIM'] },
  ],
  implementations: {
    VOLUME: {
      label: 'Gain',
      available: true,
      reason: null,
      defaults: { gain_db: 0 },
      schema: { properties: { gain_db: { type: 'number', title: 'Gain (dB)', maximum: 20 } } },
    },
    TRIM: {
      label: 'Output Trim',
      available: true,
      reason: null,
      defaults: { gain_db: 0 },
      schema: {
        properties: { gain_db: { type: 'number', title: 'Output Trim (dB)', maximum: 0 } },
      },
    },
    CHEBYSHEV: {
      label: 'Chebyshev',
      available: true,
      reason: null,
      defaults: { cutoff_hz: 30, poles: 4, ripple_db: 0, type: 1 },
      schema: {
        properties: {
          cutoff_hz: { type: 'number', title: 'Cutoff (Hz)', minimum: 1, maximum: 23999 },
          poles: { type: 'integer', title: 'Poles', minimum: 2, maximum: 32, multipleOf: 2 },
          ripple_db: { type: 'number', hidden: true },
        },
      },
    },
    LSP_EXPANDER: {
      label: 'LSP Expander Stereo',
      available: true,
      reason: null,
      defaults: { threshold_dbfs: -12, ratio: 4, attack_ms: 20 },
      schema: {
        properties: {
          threshold_dbfs: { type: 'number', title: 'Threshold (dBFS)', minimum: -120, maximum: 0 },
          ratio: { type: 'number', title: 'Ratio (N:1)' },
          attack_ms: { type: 'number', title: 'Attack (ms)' },
        },
      },
    },
    LSP_AGC: {
      label: 'LSP Autogain Stereo',
      available: false,
      reason: 'Plugin unavailable',
      defaults: { short_grow_enabled: false },
      schema: { properties: { short_grow_enabled: { type: 'boolean' } } },
    },
    LSP_EQ: {
      label: 'LSP Parametric EQ x16 Stereo',
      available: true,
      reason: null,
      defaults: { bands: [{ frequency_hz: 1000, gain_db: 0, q: 1 }] },
      schema: {
        properties: {
          bands: { type: 'array', minItems: 0, maxItems: 16, items: { $ref: '#/$defs/Band' } },
        },
        $defs: {
          Band: {
            properties: {
              frequency_hz: { type: 'number', default: 1000, title: 'Frequency (Hz)' },
              gain_db: { type: 'number', default: 0, title: 'Gain (dB)' },
              q: { type: 'number', default: 1, title: 'Q' },
            },
          },
        },
      },
    },
  },
};
// The generic field component has detailed AGC/EQ/multiband coverage separately.
for (const [key, label] of Object.entries({ LSP_GATE: 'LSP Gate Stereo',
  LSP_COMPRESSOR: 'LSP Compressor Stereo', LSP_MULTIBAND: 'LSP Multiband Compressor x8 Stereo',
  LSP_LIMITER: 'LSP Limiter Stereo' })) {
  description.implementations[key] = { label, available: true, reason: null,
    defaults: {}, schema: { properties: {} } };
}
for (const stage of description.stages) {
  description.template.stages[stage.key] = {
    selected: stage.choices[0],
    implementations: Object.fromEntries(
      stage.choices
        .filter((c) => c !== 'OFF')
        .map((c) => [c, structuredClone(description.implementations[c].defaults)]),
    ),
  };
}
const profile: ProfileResult = {
  name: 'Studio',
  path: 'custom/studio_123.json',
  kind: 'custom',
  profile: { ...structuredClone(description.template), name: 'Studio' },
};
const factory = { ...profile, kind: 'default' as const, path: 'default/basic.json' };

describe('File Audio Profiles', () => {
  let http: HttpTestingController;
  const route = { snapshot: { paramMap: convertToParamMap({}) } };
  beforeEach(() => {
    route.snapshot.paramMap = convertToParamMap({});
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ActivatedRoute, useValue: route },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  });
  afterEach(() => http.verify());
  async function list(rows: AudioProfile[] = [factory, profile], referenced = false) {
    const f = TestBed.createComponent(ProfilesList);
    f.detectChanges();
    http.expectOne(base).flush(rows);
    http
      .expectOne('/api/v1/sources')
      .flush(referenced ? [{ audio_profile_path: profile.path }] : []);
    await f.whenStable();
    f.detectChanges();
    return f;
  }
  async function editor(existing = true, saved = profile) {
    if (existing) route.snapshot.paramMap = convertToParamMap({ id: saved.path });
    const f = TestBed.createComponent(ProfileEditor);
    f.detectChanges();
    http.expectOne(base + '/dsp').flush(description);
    await Promise.resolve();
    await Promise.resolve();
    if (existing) http.expectOne(base + '/' + saved.path).flush(saved);
    await f.whenStable();
    f.detectChanges();
    return f;
  }
  it('defaults offer only Clone; customs Edit/Clone/Delete and stable paths', async () => {
    const f = await list();
    const rows = f.nativeElement.querySelectorAll('.source-row');
    expect(rows[0].textContent).toContain('default/basic.json');
    expect(rows[0].querySelectorAll('button')).toHaveLength(1);
    expect(rows[0].querySelector('button').textContent).toContain('Clone');
    expect(rows[0].querySelector('a')).toBeNull();
    expect(rows[1].querySelector('a').textContent).toContain('Edit');
    expect(rows[1].textContent).toContain('Delete');
  });
  it.each([factory, profile])('clones to the backend-returned path', async (item) => {
    const f = await list();
    vi.spyOn(window, 'prompt').mockReturnValue('New name');
    const pending = f.componentInstance.clone(item);
    const request = http.expectOne(base + '/' + item.path + '/clone');
    expect(request.request.body).toEqual({ name: 'New name' });
    request.flush({ ...profile, path: 'custom/server_generated_2.json' });
    await pending;
    expect(TestBed.inject(Router).navigate).toHaveBeenCalledWith([
      '/audio-profiles',
      'custom/server_generated_2.json',
    ]);
  });
  it('blocks referenced deletion and default editing', async () => {
    const f = await list(undefined, true);
    const deletion = Array.from(
      f.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>,
    ).find((b) => b.textContent?.trim() === 'Delete')!;
    expect(deletion.disabled).toBe(true);
    await f.componentInstance.remove(profile);
    http.expectNone(base + '/' + profile.path);
    const e = await editor(true, factory);
    expect(e.componentInstance.failed()).toBe(true);
    expect(e.nativeElement.textContent).toContain('read-only');
    expect(e.nativeElement.querySelector('form')).toBeNull();
  });
  it('reports reference race, then deletes and refreshes', async () => {
    const f = await list();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    let pending = f.componentInstance.remove(profile);
    http
      .expectOne(base + '/' + profile.path)
      .flush(
        { message: 'Profile is referenced by a Source' },
        { status: 409, statusText: 'Conflict' },
      );
    await pending;
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('referenced');
    pending = f.componentInstance.remove(profile);
    http.expectOne(base + '/' + profile.path).flush(null);
    await Promise.resolve();
    await Promise.resolve();
    http.expectOne(base).flush([factory]);
    http.expectOne('/api/v1/sources').flush([]);
    await pending;
  });
  it('handles list empty/fetch errors', async () => {
    const f = await list([]);
    expect(f.nativeElement.textContent).toContain('No audio profiles');
    const pending = f.componentInstance.load();
    http
      .expectOne(base)
      .flush({ message: 'Invalid profile file' }, { status: 422, statusText: 'Invalid' });
    http.match('/api/v1/sources').forEach((r) => {
      if (!r.cancelled) r.flush([]);
    });
    await pending;
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('Invalid profile file');
  });
  it('creates structured settings, uses actual path for subsequent saves and prevents duplicate Save', async () => {
    const f = await editor(false),
      c = f.componentInstance;
    c.form.controls.name.setValue('New');
    c.form.markAsDirty();
    const pending = c.save();
    void c.save();
    const req = http.expectOne(base + '/custom');
    expect(req.request.body.schema_version).toBe(1);
    expect(Object.keys(req.request.body.stages)).toHaveLength(11);
    expect(c.canLeave()).toBe(false);
    req.flush(profile);
    await pending;
    expect(c.path).toBe(profile.path);
    expect(c.form.pristine).toBe(true);
    const again = c.save();
    http
      .expectOne(base + '/' + profile.path)
      .flush({ ...profile, saved: true, applied: true, restart_errors: {} });
    await again;
    f.detectChanges();
    expect(f.nativeElement.querySelector('.editor-feedback').textContent).toContain('saved');
  });
  it('preserves inactive and hidden settings through switches and Save', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.document!.stages['hpf'].implementations['CHEBYSHEV']['ripple_db'] = 0.75;
    c.document!.stages['hpf'].implementations['CHEBYSHEV']['cutoff_hz'] = 80;
    c.select('hpf', 'CHEBYSHEV');
    c.select('hpf', 'OFF');
    c.select('hpf', 'CHEBYSHEV');
    expect(c.document!.stages['hpf'].implementations['CHEBYSHEV']['cutoff_hz']).toBe(80);
    c.select('hpf', 'OFF');
    const pending = c.save(),
      req = http.expectOne(base + '/' + profile.path);
    expect(req.request.body.stages.hpf.selected).toBe('OFF');
    expect(req.request.body.stages.hpf.implementations.CHEBYSHEV).toMatchObject({
      cutoff_hz: 80,
      ripple_db: 0.75,
      poles: 4,
      type: 1,
    });
    req.flush({ ...profile, saved: true, applied: true, restart_errors: {} });
    await pending;
  });
  it('retains values on validation failure and places feedback by Save', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.form.controls.name.setValue('Keep edits');
    c.form.markAsDirty();
    const pending = c.save();
    http
      .expectOne(base + '/' + profile.path)
      .flush({ message: 'Missing plugin' }, { status: 503, statusText: 'Unavailable' });
    await pending;
    f.detectChanges();
    expect(c.form.controls.name.value).toBe('Keep edits');
    expect(c.form.dirty).toBe(true);
    expect(f.nativeElement.querySelector('.editor-feedback').textContent).toContain(
      'Missing plugin',
    );
    expect(f.nativeElement.querySelector('.editor-feedback').nextElementSibling.className).toBe(
      'editor-actions',
    );
  });
  it('reports saved/apply failure distinctly without discarding saved config', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.form.markAsDirty();
    const pending = c.save();
    http.expectOne(base + '/' + profile.path).flush({
      ...profile,
      saved: true,
      applied: false,
      restart_errors: { 1: 'Audio start failed' },
    });
    await pending;
    f.detectChanges();
    expect(c.form.pristine).toBe(true);
    expect(f.nativeElement.querySelector('.editor-feedback').textContent).toContain(
      'Profile saved, but audio restart failed',
    );
    expect(TestBed.inject(Router).navigate).not.toHaveBeenCalled();
  });
  it('shows eleven sections, native units and unavailable selections honestly', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.select('gate', 'LSP_EXPANDER');
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    expect(f.nativeElement.querySelectorAll('details.stage')).toHaveLength(11);
    expect(f.nativeElement.textContent).toContain('Ratio (N:1)');
    expect(f.nativeElement.textContent).toContain('Attack (ms)');
    const unavailable = f.nativeElement.querySelector('option[value="LSP_AGC"]');
    expect(unavailable.disabled).toBe(true);
    expect(unavailable.textContent).toContain('unavailable');
    const trim = f.nativeElement.querySelectorAll('details.stage')[10].querySelector('input');
    expect(trim.max).toBe('0');
    const choices = Array.from(f.nativeElement.querySelectorAll('option') as NodeListOf<HTMLOptionElement>).map(o => o.value);
    expect(new Set(choices)).toEqual(new Set(['OFF', 'CHEBYSHEV', 'LSP_EXPANDER',
      'LSP_GATE', 'LSP_AGC', 'LSP_EQ', 'LSP_COMPRESSOR', 'LSP_MULTIBAND', 'LSP_LIMITER']));
    expect(f.nativeElement.textContent).not.toContain('Inherited');
  });
  it('EQ Add/Remove changes only that implementation and preserves Q units', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.select('eq', 'LSP_EQ');
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    const eq = f.nativeElement.querySelectorAll('details.stage')[4];
    expect(eq.textContent).toContain('Q');
    const add = Array.from(eq.querySelectorAll('button') as NodeListOf<HTMLButtonElement>).find(
      (b) => b.textContent?.trim() === 'Add band',
    )!;
    add.click();
    f.detectChanges();
    expect(c.document!.stages['eq'].implementations['LSP_EQ']['bands']).toHaveLength(2);
    (eq.querySelector('button') as HTMLButtonElement).click();
    f.detectChanges();
    expect(c.document!.stages['eq'].implementations['LSP_EQ']['bands']).toHaveLength(1);
    expect(c.form.dirty).toBe(true);
  });
});
