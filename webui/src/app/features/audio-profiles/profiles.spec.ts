import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { ProfilesList } from './profiles-list';
import { ProfileEditor } from './profile-editor';
import { AudioProfile, ProfileResult, DspDescription } from './profile-api';
import { routes } from '../../app.routes';
import { By } from '@angular/platform-browser';
import { DspFields } from './dsp-fields';
const base = '/api/v1/audio-profiles/files';
const description: DspDescription = {
  helpers: {
    hpf: 'Removes very low-frequency content; excessive cutoff can remove musical bass.',
    gate: 'Controls low-level dynamics using either expansion or gating.',
    eq: 'Adjusts tonal balance with configurable parametric EQ bands.',
    wideband: 'Controls full-band dynamics independently of the Multiband Compressor.',
    multiband:
      'Splits the audio into frequency bands and controls the dynamics of each band independently.',
    master_gain:
      'Adjusts the overall signal level. Higher gain may increase loudness and, when limiting is enabled, make the limiter work harder.',
    output_trim: 'Adjusts the final output level. Attenuation only.',
  },
  template: { schema_version: 1, name: '', description: 'Neutral settings', stages: {} },
  stages: [
    { key: 'input_gain', label: 'Profile Input Gain', choices: ['VOLUME'] },
    { key: 'hpf', label: 'HPF', choices: ['OFF', 'CHEBYSHEV'] },
    {
      key: 'gate',
      label: 'Gate / Expander',
      choices: ['OFF', 'LSP_MULTIBAND_EXPANDER', 'LSP_MULTIBAND_GATE'],
    },
    { key: 'agc', label: 'AGC / Leveller', choices: ['OFF', 'LSP_AGC'] },
    { key: 'eq', label: 'EQ', choices: ['OFF', 'LSP_EQ'] },
    { key: 'wideband', label: 'Wideband Compressor', choices: ['OFF', 'LSP_COMPRESSOR'] },
    { key: 'multiband', label: 'Multiband Compressor', choices: ['OFF', 'LSP_MULTIBAND'] },
    { key: 'master_gain', label: 'Overall Gain', choices: ['VOLUME'] },
    { key: 'limiter', label: 'Limiter', choices: ['OFF', 'LSP_LIMITER', 'FM_HEADEND_LIMITER'] },
    { key: 'lpf', label: 'LPF', choices: ['OFF', 'CHEBYSHEV'] },
    { key: 'clipper', label: 'Clipper', choices: ['OFF', 'FM_HEADEND_CLIPPER'] },
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
    LSP_MULTIBAND_EXPANDER: {
      label: 'LSP Multiband Expander x8 Stereo',
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
for (const [key, label] of Object.entries({
  LSP_MULTIBAND_GATE: 'LSP Multiband Gate x8 Stereo',
  LSP_COMPRESSOR: 'LSP Compressor Stereo',
  LSP_MULTIBAND: 'LSP Multiband Compressor x8 Stereo',
  LSP_LIMITER: 'LSP Limiter Stereo',
  FM_HEADEND_LIMITER: 'FM Headend Limiter',
  FM_HEADEND_CLIPPER: 'FM Headend Clipper',
})) {
  description.implementations[key] = {
    label,
    available: true,
    reason: null,
    defaults: {},
    schema: { properties: {} },
  };
}
for (const implementation of ['LSP_MULTIBAND_GATE', 'LSP_MULTIBAND_EXPANDER']) {
  const entry = description.implementations[implementation];
  const band = {
    bypass: {
      type: 'boolean',
      title: implementation.endsWith('GATE') ? 'Gate Bypass' : 'Expander Bypass',
      default: false,
    },
    threshold_dbfs: {
      type: 'number',
      title: 'Threshold (dBFS)',
      minimum: -60,
      maximum: 0,
      default: -24,
    },
    attack_ms: { type: 'number', title: 'Attack (ms)', minimum: 0, maximum: 2000, default: 20 },
    ...(implementation.endsWith('GATE')
      ? { reduction_db: { type: 'number', title: 'Reduction (dB attenuation)', default: 24 } }
      : { ratio: { type: 'number', title: 'Ratio (N:1)', default: 4 } }),
  };
  entry.schema = {
    properties: {
      crossovers_hz: {
        type: 'array',
        title: 'Ascending crossovers (Hz)',
        maxItems: 7,
        items: { type: 'number', minimum: 10, maximum: 20000 },
      },
      bands: {
        type: 'array',
        minItems: 8,
        maxItems: 8,
        active_count_from: 'crossovers_hz',
        items: { $ref: '#/$defs/Band' },
      },
    },
    $defs: { Band: { properties: band } },
  };
  entry.defaults = {
    crossovers_hz: [120, 1000, 5000],
    bands: Array.from({ length: 8 }, () =>
      Object.fromEntries(Object.entries(band).map(([key, value]) => [key, value.default])),
    ),
  };
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
description.implementations['FM_HEADEND_LIMITER'].schema = {
  properties: {
    threshold_dbfs: { type: 'number', title: 'Threshold (dBFS)', minimum: -60, maximum: 0 },
    lookahead_ms: { type: 'number', title: 'Lookahead (ms)', minimum: 0, maximum: 20 },
    attack_ms: { type: 'number', title: 'Attack (ms)', minimum: 0, maximum: 20 },
    release_ms: { type: 'number', title: 'Release (ms)', minimum: 1, maximum: 1000 },
    stereo_link_percent: { type: 'number', title: 'Stereo Link (%)', minimum: 0, maximum: 100 },
    true_peak_detection: { type: 'boolean', title: 'True Peak Detection', default: false },
    bypass: { type: 'boolean', hidden: true },
    weighting_us: {
      type: 'integer',
      title: 'FM detector weighting',
      default: 50,
      enum: [0, 50, 75],
      enum_labels: { '0': 'Off', '50': '50 \u00b5s', '75': '75 \u00b5s' },
    },
  },
};
description.template.stages['limiter'].implementations['FM_HEADEND_LIMITER'] = {
  threshold_dbfs: -9,
  lookahead_ms: 5,
  attack_ms: 0.25,
  release_ms: 20,
  stereo_link_percent: 100,
  weighting_us: 50,
  true_peak_detection: false,
  bypass: false,
};
description.implementations['FM_HEADEND_CLIPPER'].schema = {
  properties: {
    threshold_dbfs: { type: 'number', title: 'Threshold (dBFS)', minimum: -60, maximum: 0 },
    weighting_us: {
      type: 'integer',
      title: 'FM weighting',
      enum: [0, 50, 75],
      enum_labels: { '0': 'Off', '50': '50 \u00b5s', '75': '75 \u00b5s' },
    },
    hf_correction_us: {
      type: 'integer',
      title: 'HF Correction (\u00b5s)',
      minimum: -10,
      maximum: 30,
      default: 0,
      slider: { min: -10, max: 30, scale: 'linear', step: 1 },
    },
    clipping_mode: {
      type: 'string',
      title: 'Clipping Mode',
      enum: ['hard', 'soft'],
      enum_labels: { hard: 'Hard', soft: 'Soft' },
      default: 'hard',
    },
    soft_knee_db: {
      type: 'number',
      title: 'Soft Knee (dB)',
      minimum: 0,
      maximum: 6,
      default: 2,
      visible_when: { clipping_mode: 'soft' },
    },
    bypass: { type: 'boolean', hidden: true },
  },
};
description.template.stages['clipper'].implementations['FM_HEADEND_CLIPPER'] = {
  threshold_dbfs: -9,
  weighting_us: 50,
  hf_correction_us: 0,
  clipping_mode: 'hard',
  soft_knee_db: 2,
  bypass: false,
};
const profile: ProfileResult = {
  name: 'Studio',
  description: 'Neutral settings',
  path: 'custom/studio_123.json',
  kind: 'custom',
  profile: { ...structuredClone(description.template), name: 'Studio' },
};
const factory = { ...profile, kind: 'default' as const, path: 'default/basic.json' };

describe('File Audio Profiles', () => {
  let http: HttpTestingController;
  const route = { snapshot: { queryParamMap: convertToParamMap({}) } };
  beforeEach(() => {
    route.snapshot.queryParamMap = convertToParamMap({});
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
    if (existing) route.snapshot.queryParamMap = convertToParamMap({ path: saved.path });
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
  for (const implementation of ['LSP_MULTIBAND_GATE', 'LSP_MULTIBAND_EXPANDER']) {
    it(`edits, switches, saves and reloads ${implementation} with the shared crossover/band editor`, async () => {
      const f = await editor(),
        c = f.componentInstance;
      c.select('gate', implementation);
      f.detectChanges();
      const gate = f.debugElement.queryAll(By.css('details.stage'))[2];
      const fields = gate.query(By.directive(DspFields)).componentInstance as DspFields;
      const settings = c.document!.stages['gate'].implementations[implementation];
      expect(gate.nativeElement.querySelectorAll('details')).toHaveLength(4);
      fields.add('crossovers_hz', fields.schema().properties!['crossovers_hz']);
      f.detectChanges();
      expect(gate.nativeElement.querySelectorAll('details')).toHaveLength(5);
      fields.remove('crossovers_hz', 0);
      fields.values()['bands'][0].threshold_dbfs = -39;
      c.select(
        'gate',
        implementation.endsWith('GATE') ? 'LSP_MULTIBAND_EXPANDER' : 'LSP_MULTIBAND_GATE',
      );
      c.select('gate', implementation);
      f.detectChanges();
      expect(settings['bands']).toHaveLength(8);
      expect(settings['bands'][0].threshold_dbfs).toBe(-39);
      const saving = c.save();
      const request = http.expectOne(base + '/' + profile.path);
      const saved = { ...profile, profile: request.request.body };
      expect(saved.profile.schema_version).toBe(1);
      expect(saved.profile.description).toBe(profile.description);
      expect(saved.profile.stages.gate.implementations[implementation]).toEqual(settings);
      request.flush({ ...saved, saved: true, applied: true, restart_errors: {} });
      await saving;
      f.destroy();
      const reopened = await editor(true, saved);
      expect(
        reopened.componentInstance.document!.stages['gate'].implementations[implementation],
      ).toEqual(settings);
      expect(reopened.nativeElement.textContent).toContain('Add crossover / band');
    });
  }
  it.each([true, false])(
    'uses the shared editor layout and title (existing=%s)',
    async (existing) => {
      const f = await editor(existing);
      expect(f.nativeElement.classList.contains('editor-page')).toBe(true);
      expect(f.nativeElement.querySelector('.page-header h1').textContent).toBe(
        existing ? 'Edit Audio Profile' : 'Add Audio Profile',
      );
      expect(f.nativeElement.querySelector('section.editor-panel')).not.toBeNull();
      expect(f.nativeElement.querySelector('details.stage.editor-panel')).not.toBeNull();
      expect(f.nativeElement.querySelector('.editor-actions')).not.toBeNull();
    },
  );
  it('loads, edits and saves description with dirty-state tracking', async () => {
    const f = await editor(),
      c = f.componentInstance;
    const input = f.nativeElement.querySelector('[formControlName=description]');
    expect(input.value).toBe(profile.description);
    input.value = 'Updated description';
    input.dispatchEvent(new Event('input'));
    expect(c.form.dirty).toBe(true);
    const pending = c.save();
    const request = http.expectOne(base + '/' + profile.path);
    expect(request.request.body.description).toBe('Updated description');
    expect(request.request.body.stages).toEqual(profile.profile.stages);
    const saved = { ...profile, description: 'Updated description', profile: request.request.body };
    request.flush(saved);
    await pending;
    expect(c.form.pristine).toBe(true);
    f.destroy();
    const reopened = await editor(true, saved);
    expect(reopened.nativeElement.querySelector('[formControlName=description]').value).toBe(
      'Updated description',
    );
  });
  it('shows documentation only for the selected implementation and keeps fixed modes internal', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.description!.implementations['LSP_MULTIBAND_GATE'].documentation_url =
      'https://lsp-plug.in/?page=manuals&section=mb_gate_stereo';
    c.description!.implementations['LSP_MULTIBAND_EXPANDER'].documentation_url =
      'https://lsp-plug.in/?page=manuals&section=mb_expander_stereo';
    c.select('gate', 'LSP_MULTIBAND_GATE');
    f.detectChanges();
    const gate = f.nativeElement.querySelectorAll('details.stage')[2];
    expect(gate.querySelector('a').href).toContain('section=mb_gate_stereo');
    expect(gate.querySelector('a').rel).toBe('noopener noreferrer');
    expect(gate.querySelector('a').target).toBe('_blank');
    c.select('gate', 'LSP_MULTIBAND_EXPANDER');
    f.detectChanges();
    expect(gate.querySelector('a').href).toContain('section=mb_expander_stereo');
    c.select('gate', 'OFF');
    f.detectChanges();
    expect(gate.querySelector('a')).toBeNull();
    expect(f.nativeElement.textContent).toContain('Overall Gain');
    for (const helper of Object.values(description.helpers!)) {
      expect(f.nativeElement.textContent).toContain(helper);
    }
    expect(f.nativeElement.textContent).not.toContain('Master Gain');
    expect(f.nativeElement.textContent).not.toContain('Save restarts only active Sources');
    expect(f.nativeElement.textContent).not.toContain('No software pre-emphasis');
    expect(f.nativeElement.textContent).not.toContain('Classic');
    expect(f.nativeElement.textContent).toContain('FM pre-emphasis is handled by the TX');
  });
  it('refuses out-of-range precise entry without changing it or sending Save', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.document!.stages['output_trim'].implementations['TRIM']['gain_db'] = 1;
    await c.save();
    http.expectNone(base + '/' + profile.path);
    expect(c.error()).toContain('supported limits');
    expect(c.document!.stages['output_trim'].implementations['TRIM']['gain_db']).toBe(1);
  });
  it('defaults offer View/Clone; customs Edit/Clone/Delete and stable paths', async () => {
    const f = await list();
    const rows = f.nativeElement.querySelectorAll('.source-row');
    expect(rows[0].textContent).toContain('default/basic.json');
    for (const row of rows) {
      expect(
        [...row.querySelectorAll('span')].some((el: any) =>
          ['default', 'custom'].includes(el.textContent.trim()),
        ),
      ).toBe(false);
    }
    expect(rows[0].querySelectorAll('button')).toHaveLength(1);
    expect(rows[0].querySelector('button').textContent).toContain('Clone');
    expect(rows[0].querySelector('.list-actions a').textContent).toContain('View');
    expect(rows[0].textContent).toContain(factory.description);
    expect(rows[1].textContent).toContain(profile.description);
    expect(rows[1].querySelector('.list-actions a').textContent).toContain('Edit');
    expect(rows[1].textContent).toContain('Delete');
  });
  it('uses four shared columns and type strips while preserving usage and permissions', async () => {
    const f = await list(undefined, true);
    const rows = [...f.nativeElement.querySelectorAll('.list-card')] as HTMLElement[];
    expect(rows.map((row) => row.dataset['profileKind'])).toEqual(['default', 'custom']);
    for (const [i, item] of [factory, profile].entries()) {
      const row = rows[i];
      expect(getComputedStyle(row).getPropertyValue('--list-strip-color').trim()).toBe(
        i === 0 ? '#6f9fd8' : '#8f84d9',
      );
      expect(row.classList.contains('list-grid--four')).toBe(true);
      expect(row.querySelector('.list-identity .list-primary')?.textContent).toBe(item.name);
      expect(row.querySelector('.list-identity .list-secondary')?.textContent).toBe(item.path);
      expect(row.querySelector('.list-description')?.textContent).toBe(item.description);
      expect(row.querySelector('.list-status')?.textContent).toContain(`Used by ${i} sources`);
      expect(row.querySelector('.list-actions')).not.toBeNull();
      const nameLink = row.querySelector('.list-identity a')!;
      expect(nameLink.getAttribute('href')).toBe(
        row.querySelector('.list-actions a')!.getAttribute('href'),
      );
      expect(nameLink.getAttribute('href')).toBe(
        '/audio-profiles/edit?path=' + encodeURIComponent(item.path),
      );
    }
    const deletion = [...rows[1].querySelectorAll('button')].find(
      (button) => button.textContent?.trim() === 'Delete',
    )!;
    expect(deletion.disabled).toBe(true);
  });
  it.each([factory, profile])('clones to the backend-returned path', async (item) => {
    const f = await list();
    vi.spyOn(window, 'prompt').mockReturnValue('New name');
    const pending = f.componentInstance.clone(item);
    const request = http.expectOne(base + '/' + item.path + '/clone');
    expect(request.request.body).toEqual({ name: 'New name' });
    const cloned = {
      ...item,
      kind: 'custom' as const,
      name: 'New name',
      path: 'custom/server_generated_2.json',
      profile: { ...item.profile, name: 'New name' },
    };
    request.flush(cloned);
    await pending;
    expect(TestBed.inject(Router).navigate).toHaveBeenCalledWith(['/audio-profiles/edit'], {
      queryParams: { path: 'custom/server_generated_2.json' },
    });
    const opened = await editor(true, cloned);
    expect(opened.componentInstance.form.controls.description.value).toBe(item.description);
    expect(opened.componentInstance.document!.description).toBe(item.description);
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
    expect(e.componentInstance.failed()).toBe(false);
    expect(e.nativeElement.textContent).toContain('read-only');
    expect(e.nativeElement.querySelector('[formControlName=description]').value).toBe(
      factory.description,
    );
    expect(e.componentInstance.form.disabled).toBe(true);
    expect(e.nativeElement.querySelector('fieldset').disabled).toBe(true);
    await e.componentInstance.save();
    http.expectNone(base + '/' + factory.path);
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
    expect(Object.keys(req.request.body.stages)).toHaveLength(12);
    expect(c.canLeave()).toBe(false);
    req.flush(profile);
    await pending;
    expect(c.path).toBe(profile.path);
    expect(TestBed.inject(Router).navigate).toHaveBeenCalledWith(['/audio-profiles/edit'], {
      queryParams: { path: profile.path },
      replaceUrl: true,
      state: { profileSaved: true },
    });
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
  it('shows twelve sections, native units and unavailable selections honestly', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.select('gate', 'LSP_MULTIBAND_EXPANDER');
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    expect(f.nativeElement.querySelectorAll('details.stage')).toHaveLength(12);
    expect(f.nativeElement.textContent).toContain('Ratio (N:1)');
    expect(f.nativeElement.textContent).toContain('Attack (ms)');
    const unavailable = f.nativeElement.querySelector('option[value="LSP_AGC"]');
    expect(unavailable.disabled).toBe(true);
    expect(unavailable.textContent).toContain('unavailable');
    const trim = f.nativeElement.querySelectorAll('details.stage')[11].querySelector('input');
    expect(trim.max).toBe('0');
    const choices = Array.from(
      f.nativeElement.querySelectorAll('option') as NodeListOf<HTMLOptionElement>,
    ).map((o) => o.value);
    expect(new Set(choices)).toEqual(
      new Set([
        'OFF',
        'CHEBYSHEV',
        'LSP_MULTIBAND_EXPANDER',
        'LSP_MULTIBAND_GATE',
        'LSP_AGC',
        'LSP_EQ',
        'LSP_COMPRESSOR',
        'LSP_MULTIBAND',
        'LSP_LIMITER',
        'FM_HEADEND_LIMITER',
        'FM_HEADEND_CLIPPER',
      ]),
    );
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
  it('deep links keep the full profile path in a query, including after a fresh load', async () => {
    const router = TestBed.inject(Router);
    const path = 'custom/teszt2_20260928180555.json';
    const url = router.serializeUrl(
      router.createUrlTree(['/audio-profiles/edit'], { queryParams: { path } }),
    );
    expect(url.split('?')[0]).toBe('/audio-profiles/edit');
    const parsed = router.parseUrl(url);
    expect(parsed.queryParamMap.get('path')).toBe(path);
    expect(routes.find((r) => r.path === 'audio-profiles/edit')?.component).toBe(ProfileEditor);
    route.snapshot.queryParamMap = parsed.queryParamMap;
    const f = await editor(true, { ...profile, path });
    expect(f.componentInstance.path).toBe(path);
    const pending = f.componentInstance.save();
    http.expectOne(base + '/' + path).flush({ ...profile, path });
    await pending;
    const listFixture = await list([{ ...profile, path }]);
    expect(listFixture.nativeElement.querySelector('.source-row a').getAttribute('href')).toBe(url);
  });
  it('edits weighting as labelled choices and preserves it with the limiter off', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.select('limiter', 'FM_HEADEND_LIMITER');
    f.detectChanges();
    await f.whenStable();
    const section = f.nativeElement.querySelectorAll('details.stage')[8];
    const select = section.querySelector('app-dsp-fields select') as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent?.trim())).toEqual([
      'Off',
      '50 µs',
      '75 µs',
    ]);
    select.selectedIndex = 2;
    select.dispatchEvent(new Event('change'));
    await f.whenStable();
    expect(
      c.document!.stages['limiter'].implementations['FM_HEADEND_LIMITER']['weighting_us'],
    ).toBe(75);
    for (const label of [
      'Threshold (dBFS)',
      'Lookahead (ms)',
      'Attack (ms)',
      'Release (ms)',
      'Stereo Link (%)',
    ]) {
      expect(section.textContent).toContain(label);
    }
    const peak = section.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(peak.checked).toBe(false);
    expect(section.textContent).toContain('True Peak Detection');
    peak.click();
    await f.whenStable();
    c.select('limiter', 'LSP_LIMITER');
    f.detectChanges();
    expect(section.textContent).not.toContain('FM detector weighting');
    expect(section.textContent).not.toContain('FM limiter weighting');
    c.select('limiter', 'FM_HEADEND_LIMITER');
    expect(
      c.document!.stages['limiter'].implementations['FM_HEADEND_LIMITER']['weighting_us'],
    ).toBe(75);
    c.select('limiter', 'OFF');
    const pending = c.save();
    const request = http.expectOne(base + '/' + profile.path);
    expect(request.request.body.stages.limiter.selected).toBe('OFF');
    expect(
      request.request.body.stages.limiter.implementations.FM_HEADEND_LIMITER.true_peak_detection,
    ).toBe(true);
    expect(
      request.request.body.stages.limiter.implementations.FM_HEADEND_LIMITER.weighting_us,
    ).toBe(75);
    request.flush({ ...profile, profile: request.request.body });
    await pending;
  });
  it('renders Clipper after LPF and round-trips it independently of Limiter', async () => {
    const f = await editor(),
      c = f.componentInstance;
    const original = structuredClone(c.document!.stages);
    expect(original['clipper'].selected).toBe('OFF');
    const keys = c.description!.stages.map((s) => s.key);
    expect(keys.slice(-4)).toEqual(['limiter', 'lpf', 'clipper', 'output_trim']);
    c.select('clipper', 'FM_HEADEND_CLIPPER');
    f.detectChanges();
    await f.whenStable();
    const section = f.nativeElement.querySelectorAll('details.stage')[10];
    expect(section.textContent).toContain('Clipper');
    expect(section.textContent).toContain('Threshold (dBFS)');
    const select = section.querySelector('app-dsp-fields select') as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent?.trim())).toEqual([
      'Off',
      '50 \u00b5s',
      '75 \u00b5s',
    ]);
    select.selectedIndex = 2;
    select.dispatchEvent(new Event('change'));
    await f.whenStable();
    expect(
      c.document!.stages['clipper'].implementations['FM_HEADEND_CLIPPER']['weighting_us'],
    ).toBe(75);
    const mode = section.querySelectorAll('app-dsp-fields select')[1] as HTMLSelectElement;
    expect([...mode.options].map((o) => o.textContent?.trim())).toEqual(['Hard', 'Soft']);
    expect(mode.selectedIndex).toBe(0);
    expect(section.textContent).not.toContain('Soft Knee (dB)');
    mode.selectedIndex = 1;
    mode.dispatchEvent(new Event('change'));
    await f.whenStable();
    const knee = [...section.querySelectorAll('app-dsp-number')].find((el: any) =>
      el.textContent.includes('Soft Knee (dB)'),
    ) as HTMLElement;
    const input = knee.querySelector('input[type="number"]') as HTMLInputElement;
    expect(input.value).toBe('2');
    expect(input.min).toBe('0');
    expect(input.max).toBe('6');
    input.value = '4.5';
    input.dispatchEvent(new Event('input'));
    await f.whenStable();
    mode.selectedIndex = 0;
    mode.dispatchEvent(new Event('change'));
    await f.whenStable();
    expect(section.textContent).not.toContain('Soft Knee (dB)');
    expect(
      c.document!.stages['clipper'].implementations['FM_HEADEND_CLIPPER']['soft_knee_db'],
    ).toBe(4.5);
    mode.selectedIndex = 1;
    mode.dispatchEvent(new Event('change'));
    await f.whenStable();
    expect(section.textContent).toContain('Soft Knee (dB)');
    c.select('clipper', 'OFF');
    const pending = c.save();
    const request = http.expectOne(base + '/' + profile.path);
    expect(request.request.body.stages.clipper.selected).toBe('OFF');
    expect(
      request.request.body.stages.clipper.implementations.FM_HEADEND_CLIPPER.clipping_mode,
    ).toBe('soft');
    expect(
      request.request.body.stages.clipper.implementations.FM_HEADEND_CLIPPER.soft_knee_db,
    ).toBe(4.5);
    expect(
      request.request.body.stages.clipper.implementations.FM_HEADEND_CLIPPER.weighting_us,
    ).toBe(75);
    for (const key of keys.filter((k) => k !== 'clipper'))
      expect(request.request.body.stages[key]).toEqual(original[key]);
    request.flush({ ...profile, profile: request.request.body });
    await pending;
  });
});
