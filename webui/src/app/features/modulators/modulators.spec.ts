import { SystemApi } from '../../core/api/system-api';
import { systemInfo } from '../system/test-fixtures';
import { defer, of, Subject } from 'rxjs';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, Router, convertToParamMap, provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ModulatorEditor, modulatorDirtyGuard } from './modulator-editor';
import { ModulatorsList } from './modulators-list';
import { mhz, khz, parseHex, hex, ptyLabels, rdsText, afOptions, textLength } from './values';
import { DashboardState, compose } from '../dashboard/dashboard-state';
import { sample } from '../dashboard/test-fixtures';
import { Modulator } from '../../core/api/models';

describe('Modulator value conversions', () => {
  it.each([
    [100700, '100.7'],
    [87600, '87.6'],
    [107900, '107.9'],
    [98101, '98.101'],
  ])('converts %s kHz exactly', (frequency, display) => {
    expect(mhz(Number(frequency))).toBe(display);
    expect(khz(String(display))).toBe(frequency);
  });
  it.each(['1e2', '98.1001', '-98', '0', 'NaN'])('rejects ambiguous MHz %s', (value) =>
    expect(() => khz(value)).toThrow(),
  );
  it('supports an unconfigured frequency', () => {
    expect(khz('', true)).toBeNull();
    expect(mhz(null)).toBe('');
  });
  it('parses and normalizes hex without decimal UI', () => {
    expect(parseHex('0xb0d7', 4)).toBe(45271);
    expect(hex(45271, 4)).toBe('B0D7');
    expect(hex(1, 4)).toBe('0001');
    expect(parseHex('E0', 2, true)).toBe(224);
    expect(parseHex('', 2, true)).toBeNull();
    for (const value of ['10000', '-1', 'xyz']) expect(() => parseHex(value, 4)).toThrow();
    expect(() => parseHex('100', 2)).toThrow();
  });
  it('keeps Unicode in Fill from source and leaves encoding to Linux', () => {
    const name = 'Caf\u00e9 \u2014 Stra\u00dfe \u00c6\u00d8';
    expect(rdsText(name, 64)).toBe(name);
    expect(rdsText('$^`~\u{1f600}', 64)).toBe('$^`~\u{1f600}');
    expect(rdsText('Cafe\u0301', 8)).toBe('Caf\u00e9');
    expect(rdsText('\u00e1\u00e9\u00ed\u00f3\u00f6\u0151\u00fa\u00fc\u0171', 8)).toBe(
      '\u00e1\u00e9\u00ed\u00f3\u00f6\u0151\u00fa\u00fc',
    );
    expect(rdsText('Klubradio Budapest Livestream', 8)).toBe('Klubradi');
    expect(rdsText('x'.repeat(100), 64)).toHaveLength(64);
    expect(rdsText('A\nB\u001b\u00a0', 64)).toBe('A?B??');
    expect(rdsText('x'.repeat(7) + '\u{1f600}tail', 8)).toBe('x'.repeat(7) + '\u{1f600}');
  });
  it('has exactly 30 European programme types', () => {
    expect(ptyLabels).toHaveLength(30);
    expect(ptyLabels[5]).toBe('Education');
    expect(ptyLabels[18]).toBe("Children's Programmes");
    expect(ptyLabels[29]).toBe('Documentary');
  });
});

describe('Modulator configuration', () => {
  const routeSnapshot = { paramMap: convertToParamMap({ id: '0' }) };
  const routeStub = { snapshot: routeSnapshot, paramMap: defer(() => of(routeSnapshot.paramMap)) };
  let http: HttpTestingController;
  let shared: DashboardState;
  beforeEach(() => {
    routeStub.snapshot.paramMap = convertToParamMap({ id: '0' });
    routeStub.paramMap = defer(() => of(routeSnapshot.paramMap));
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: SystemApi, useValue: { info: () => of(systemInfo) } },
        { provide: ActivatedRoute, useValue: routeStub },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    shared = TestBed.inject(DashboardState);
    const data = sample();
    shared.data.set(data);
    shared.channels.set(compose(data));
    shared.stale.set(false);
    shared.loading.set(false);
    vi.spyOn(shared, 'refreshAfterMutation').mockResolvedValue();
  });
  afterEach(() => http.verify());
  async function editor(changes: Partial<Modulator> = {}) {
    const f = TestBed.createComponent(ModulatorEditor);
    f.detectChanges();
    http.expectOne('/api/v1/modulators/0').flush({ ...sample().modulators[0], ...changes });
    await f.whenStable();
    f.detectChanges();
    return f;
  }
  it('offers exactly the board analog gains and preserves disabled values', async () => {
    const f = await editor({
      audio_mode: 'ANALOG',
      input_source_id: null,
      gain_mode: 'MANUAL',
      analog_gain_db: 6,
    });
    const c = f.componentInstance;
    const select = f.nativeElement.querySelector(
      '[formControlName="analog_gain_db"]',
    ) as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => Number(o.value))).toEqual([
      -1.5, 0, 1.5, 3, 4.5, 6, 7.5, 9, 10.5, 12, 13.5, 15,
    ]);
    expect(select.options[0].textContent).toContain('-1.5 dB');
    expect(select.options[11].textContent).toContain('+15.0 dB');
    expect(select.disabled).toBe(false);
    c.form.controls.gain_mode.setValue('AUTO');
    c.syncGain();
    f.detectChanges();
    expect(select.disabled).toBe(true);
    expect(c.draft().analog_gain_db).toBe(6);
    c.form.controls.audio_mode.setValue('DIGITAL');
    c.syncGain();
    f.detectChanges();
    expect(c.form.controls.analog_gain_db.disabled).toBe(true);
    expect(c.form.controls.analog_gain_db.value).toBe('6');
    expect(c.form.controls.soft_clipping.enabled).toBe(true);
  });
  it('hides one PS duration without resetting it and counts validated code points', async () => {
    const f = await editor({ rds_ps: [{ text: 'Station', duration_s: 17 }] });
    const c = f.componentInstance;
    expect(f.nativeElement.querySelector('[formControlName="duration_s"]')).toBeNull();
    c.addPs();
    f.detectChanges();
    expect(f.nativeElement.querySelectorAll('[formControlName="duration_s"]')).toHaveLength(2);
    f.nativeElement.querySelector('[aria-label="Remove PS 2"]').click();
    f.detectChanges();
    expect(f.nativeElement.querySelector('[formControlName="duration_s"]')).toBeNull();
    expect(c.draft().rds_override_profile!.rds_ps[0].duration_s).toBe(17);
    expect(f.nativeElement.textContent).toContain('7 / 8');
    const rt = f.nativeElement.querySelector('[formControlName="rds_rt"]') as HTMLTextAreaElement;
    rt.value = 'abc';
    rt.dispatchEvent(new Event('input'));
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('3 / 64');
    expect(textLength('A\u{1f600}')).toBe(2);
    expect(f.nativeElement.querySelectorAll('.rds-text')).toHaveLength(4);
  });
  it('offers only Method A AF frequencies and preserves integer kHz on save', async () => {
    const f = await editor({ rds_af_khz: [87600, 107900] });
    const c = f.componentInstance;
    expect(afOptions).toHaveLength(204);
    expect(afOptions[0]).toEqual({ value: 87600, label: '87.6 MHz' });
    expect(afOptions[203]).toEqual({ value: 107900, label: '107.9 MHz' });
    afOptions.forEach((o, i) => expect(o.value).toBe(87600 + i * 100));
    expect(f.nativeElement.querySelectorAll('.af-entry select')).toHaveLength(2);
    expect(c.draft().rds_override_profile!.rds_af_khz).toEqual([87600, 107900]);
    c.af.at(0).setValue('98.1');
    expect(c.draft().rds_override_profile!.rds_af_khz).toEqual([98100, 107900]);
  });
  it('uses concise RF wording without a digital gain control', async () => {
    const f = await editor();
    const el = f.nativeElement as HTMLElement;
    expect(el.textContent).not.toContain('One source may feed multiple transmitters.');
    expect(el.textContent).toContain('Relative to maximum output.');
    expect(el.textContent).not.toContain('Absolute output calibration');
    expect(el.querySelector('[formControlName="digital_trim_db"]')).toBeNull();
  });
  it('renders eight fixed IDs, summaries, runtime state and edit destinations', () => {
    const f = TestBed.createComponent(ModulatorsList);
    f.detectChanges();
    expect(f.nativeElement.querySelectorAll('section.summary')).toHaveLength(8);
    for (let id = 0; id < 8; id++)
      expect(f.nativeElement.querySelector(`a[href="/modulators/${id}"]`).textContent).toContain(
        `TX ${id}`,
      );
    for (let id = 0; id < 8; id++) {
      const row = f.nativeElement.querySelectorAll('section.summary')[id];
      expect(row.querySelector('.list-identity a').getAttribute('href')).toBe(`/modulators/${id}`);
      expect(row.querySelector('.list-identity a').getAttribute('href')).toBe(
        row.querySelector('.list-actions a').getAttribute('href'),
      );
    }
    expect(f.nativeElement.textContent).toContain('91.6 MHz');
    expect(f.nativeElement.textContent).toContain('Studio feed');
    expect(f.nativeElement.textContent).toContain('Reported state: ON');
    expect(f.nativeElement.textContent).toContain('RDS On');
    shared.stale.set(true);
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('UNKNOWN');
  });
  it.each(['ON', 'OFF', 'ERROR', 'UNKNOWN', 'STARTING', null])(
    'uses reported %s for the strip and label regardless of desired state',
    (reported) => {
      const data = sample();
      data.modulators[0].enabled = reported !== 'ON';
      data.status.modulators[0].tx_state = reported === 'ON' ? 'OFF' : 'ON';
      data.status.modulators[0].hardware_tx_state = reported;
      shared.channels.set(compose(data));
      const f = TestBed.createComponent(ModulatorsList);
      f.detectChanges();
      const row = f.nativeElement.querySelector('section');
      expect(row.dataset.reportedState).toBe(reported ?? 'UNKNOWN');
      const color =
        reported === 'ON'
          ? 'success'
          : reported === 'OFF'
            ? 'inactive'
            : reported === 'ERROR'
              ? 'error'
              : 'warning';
      expect(getComputedStyle(row).getPropertyValue('--list-strip-color').trim()).toBe(
        `var(--${color})`,
      );
      expect(row.querySelector('.list-status').textContent).toContain(reported ?? 'UNKNOWN');
      expect(row.querySelector('.list-description .list-primary').textContent).toBe('Studio feed');
      expect(row.querySelector('.list-actions a').getAttribute('href')).toBe('/modulators/0');
      shared.stale.set(true);
      f.detectChanges();
      expect(row.dataset.reportedState).toBe('UNKNOWN');
      expect(getComputedStyle(row).getPropertyValue('--list-strip-color').trim()).toBe(
        'var(--warning)',
      );
    },
  );
  it('does not present stale RP ON as confirmed and handles unassigned sources', () => {
    const data = sample();
    data.status.system.controller_observation_stale = true;
    data.modulators[0].input_source_id = null;
    shared.channels.set(compose(data));
    const f = TestBed.createComponent(ModulatorsList);
    f.detectChanges();
    const row = f.nativeElement.querySelector('section');
    expect(row.dataset.reportedState).toBe('UNKNOWN');
    expect(row.querySelector('.list-description .list-primary').textContent).toBe(
      'No source assigned',
    );
  });
  it('loads all three sections in order without editable identity or desired state', async () => {
    const f = await editor();
    expect(
      Array.from(f.nativeElement.querySelectorAll('section h2')).map(
        (el) => (el as HTMLElement).textContent,
      ),
    ).toEqual(['RF', 'Audio', 'RDS']);
    expect(f.nativeElement.classList.contains('editor-wide')).toBe(true);
    expect(f.nativeElement.querySelector('.editor-actions')).not.toBeNull();
    expect(f.componentInstance.form.controls.frequency_khz.value).toBe(91600);
    expect(f.componentInstance.form.pristine).toBe(true);
    expect(f.nativeElement.querySelector('[formControlName="name"]')).toBeNull();
    expect(f.nativeElement.querySelector('[formControlName="enabled"]')).toBeNull();
    expect(f.nativeElement.querySelector('[formControlName="input_source_id"]')).not.toBeNull();
  });
  it('preserves a stored frequency outside the active plan', async () => {
    const f = await editor({ frequency_khz: 76050 });
    const c = f.componentInstance;
    expect(c.form.controls.frequency_khz.value).toBe(76050);
    expect(c.draft().frequency_khz).toBe(76050);
    expect(f.nativeElement.textContent).toContain('outside this plan');
    expect(c.frequencies[0].label).toContain('stored, outside plan');
    expect(afOptions[0].value).toBe(87600);
  });
  it('uses an API supplied 50 kHz plan', async () => {
    vi.spyOn(TestBed.inject(SystemApi), 'info').mockReturnValue(
      of({
        ...systemInfo,
        frequency_plan: {
          region: 0,
          region_name: 'Expert',
          fm_min_khz: 76000,
          fm_max_khz: 108000,
          fm_step_khz: 50,
        },
      }),
    );
    const f = await editor();
    expect(f.componentInstance.frequencies).toHaveLength(641);
    expect(f.componentInstance.frequencies[1]).toEqual({ value: 76050, label: '76.05 MHz' });
    expect(afOptions).toHaveLength(204);
  });
  it('requires explicit frequency selection and offers the integer European raster', async () => {
    const f = await editor({ frequency_khz: null }),
      c = f.componentInstance;
    const select = f.nativeElement.querySelector(
      '[formControlName="frequency_khz"]',
    ) as HTMLSelectElement;
    expect(c.form.invalid).toBe(true);
    expect(c.form.controls.frequency_khz.value).toBeNull();
    expect(select.options[0].disabled).toBe(true);
    expect(Array.from(select.options).filter((o) => !o.disabled)).toHaveLength(206);
    expect(c.frequencies[0]).toEqual({ value: 87500, label: '87.5 MHz' });
    expect(c.frequencies[205]).toEqual({ value: 108000, label: '108.0 MHz' });
    c.frequencies.forEach((o, i) => {
      expect(o.value).toBe(87500 + i * 100);
      expect(o.label).toMatch(/^\d+\.\d MHz$/);
    });
    expect(() => c.draft()).toThrow('Select a frequency');
    c.form.controls.frequency_khz.setValue(100700);
    expect(c.draft().frequency_khz).toBe(100700);
  });
  it('offers exactly 16 relative RF levels without inventing an absolute value', async () => {
    const f = await editor({ rf_attenuation_db: null }),
      c = f.componentInstance;
    expect(c.form.invalid).toBe(true);
    expect(() => c.draft()).toThrow('Select RF output');
    const select = f.nativeElement.querySelector(
      '[formControlName="rf_attenuation_db"]',
    ) as HTMLSelectElement;
    expect(select.options[0].disabled).toBe(true);
    expect(Array.from(select.options).filter((o) => !o.disabled)).toHaveLength(16);
    expect(c.rfLevels[0]).toEqual({ value: 0, label: 'Maximum' });
    expect(c.rfLevels[1]).toEqual({ value: 1.5, label: '-1.5 dB' });
    expect(c.rfLevels[15]).toEqual({ value: 22.5, label: '-22.5 dB' });
    c.rfLevels.forEach((o) => {
      c.form.controls.rf_attenuation_db.setValue(o.value);
      expect(c.draft().rf_attenuation_db).toBe(o.value);
    });
    expect(f.nativeElement.textContent).not.toContain('dB\u00b5V');
  });
  it('saves RF/audio/RDS with exact API names and returns to pristine', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.form.patchValue({
      frequency_khz: 100700,
      rf_attenuation_db: 6,
      stereo: false,
      preemphasis_us: 75,
      pi: '0xb0d7',
      ecc: 'e0',
      rds_tp: true,
      rds_rt: 'Fallback',
      rds_metadata_to_rt: true,
    });
    c.form.markAsDirty();
    const pending = c.save();
    const req = http.expectOne('/api/v1/modulators/0/save-and-resume');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toMatchObject({
      frequency_khz: 100700,
      rf_attenuation_db: 6,
      stereo: false,
      preemphasis_us: 75,
      rds_override_profile: {
        rds_pi: 45271,
        rds_ecc: 224,
        rds_tp: true,
        rds_rt: 'Fallback',
        rds_metadata_to_rt: true,
      },
    });
    expect(req.request.body.enabled).toBeUndefined();
    expect(req.request.body.name).toBeUndefined();
    req.flush({
      saved: true,
      stop_confirmed: true,
      restarted: true,
      failed_stage: null,
      message: 'Configuration saved and TX restarted.',
      configuration: { ...sample().modulators[0], ...req.request.body },
    });
    await pending;
    expect(c.form.pristine).toBe(true);
    expect(c.form.controls.pi.value).toBe('B0D7');
    expect(shared.refreshAfterMutation).toHaveBeenCalledOnce();
  });
  it('preserves edits on active save rejection and never issues lifecycle requests', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.form.controls.frequency_khz.setValue(99500);
    c.form.markAsDirty();
    const pending = c.save();
    await c.save();
    const req = http.expectOne('/api/v1/modulators/0/save-and-resume');
    req.flush(
      {
        error: 'MODULATOR_ACTIVE',
        message: 'Stop the transmitter before editing its configuration',
      },
      { status: 409, statusText: 'Conflict' },
    );
    await pending;
    expect(c.form.controls.frequency_khz.value).toBe(99500);
    expect(c.form.dirty).toBe(true);
    expect(c.error()).toContain('Stop the transmitter');
    http.expectNone((r) => r.url.endsWith('/stop') || r.url.endsWith('/start'));
  });
  it('preserves validation error details', async () => {
    const f = await editor(),
      c = f.componentInstance;
    const pending = c.save();
    http
      .expectOne('/api/v1/modulators/0/save-and-resume')
      .flush(
        { error: 'INVALID_FREQUENCY', message: 'Frequency must be inside the configured FM band' },
        { status: 422, statusText: 'Validation' },
      );
    await pending;
    expect(c.error()).toContain('configured FM band');
  });
  it.each(['stop', 'save', 'start'] as const)(
    'retains accurate %s stage results beside Save',
    async (stage) => {
      const f = await editor(),
        c = f.componentInstance;
      c.form.patchValue({ frequency_khz: 99500, rds_rt: 'Edited text' });
      c.form.markAsDirty();
      const before = c.form.getRawValue();
      const pending = c.save();
      await c.save();
      await c.loadConfig();
      expect(c.canLeave()).toBe(false);
      shared.data.set(sample());
      shared.channels.set(compose(sample()));
      f.detectChanges();
      expect(c.form.getRawValue()).toEqual(before);
      const req = http.expectOne('/api/v1/modulators/0/save-and-resume');
      const message =
        stage === 'start'
          ? 'Configuration saved, but TX restart failed'
          : `${stage} failed; configuration not saved`;
      req.flush({
        saved: stage === 'start',
        stop_confirmed: stage !== 'stop',
        restarted: false,
        failed_stage: stage,
        message,
        configuration:
          stage === 'start' ? { ...sample().modulators[0], ...req.request.body } : null,
      });
      await pending;
      f.detectChanges();
      expect(c.form.controls.frequency_khz.value).toBe(99500);
      expect(c.form.controls.rds_rt.value).toBe('Edited text');
      expect(c.form.dirty).toBe(stage !== 'start');
      expect(f.nativeElement.querySelector('.editor-feedback').textContent).toContain(message);
      expect(f.nativeElement.querySelectorAll('[role="alert"]')).toHaveLength(1);
      http.expectNone((r) => r.url.endsWith('/stop') || r.url.endsWith('/start'));
    },
  );
  it('groups PS text and duration above one inline action row', async () => {
    const f = await editor(),
      c = f.componentInstance;
    let row = f.nativeElement.querySelector('.list-entry');
    expect(row.querySelector('.ps-fields').classList.contains('single-ps')).toBe(true);
    expect(row.querySelector('[formControlName="duration_s"]')).toBeNull();
    c.addPs();
    f.detectChanges();
    const rows = [...f.nativeElement.querySelectorAll('.list-entry')];
    for (const r of rows) {
      expect(r.querySelector('.ps-fields [formControlName="text"]').maxLength).toBe(8);
      expect(r.querySelector('.ps-fields [formControlName="duration_s"]')).not.toBeNull();
      expect(
        [...r.querySelectorAll('.actions button')].map((b: any) => b.textContent.trim()),
      ).toEqual(['Up', 'Down', 'Remove']);
    }
    expect(rows[0].querySelector('.actions button').disabled).toBe(true);
    expect(rows[1].querySelectorAll('.actions button')[1].disabled).toBe(true);
    c.ps.at(1).controls.text.setValue('SECOND');
    rows[1].querySelector('.actions button').click();
    f.detectChanges();
    expect(c.ps.at(0).controls.text.value).toBe('SECOND');
  });
  it('loads and saves disabled RT without erasing PAD or text', async () => {
    const f = await editor({
        rds_rt_enabled: true,
        rds_rt_termination: 'PAD',
        rds_rt: 'Keep text',
      }),
      c = f.componentInstance;
    c.form.controls.rds_rt_enabled.setValue(false);
    c.form.markAsDirty();
    f.detectChanges();
    expect(f.nativeElement.textContent).not.toContain('Default Radio Text');
    expect(f.nativeElement.textContent).toContain('Radio Text (RT) enabled');
    expect(f.nativeElement.textContent).not.toContain('RT termination');
    expect(f.nativeElement.querySelector('[formControlName="rds_metadata_to_rt"]')).toBeNull();
    const rtToggle = f.nativeElement.querySelector('[formControlName="rds_rt_enabled"]');
    rtToggle.checked = true;
    rtToggle.dispatchEvent(new Event('change', { bubbles: true }));
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    expect(f.nativeElement.querySelector('[formControlName="rds_rt"]').value).toBe('Keep text');
    expect(f.nativeElement.querySelector('[formControlName="rds_rt_termination"]').value).toBe(
      'PAD',
    );
    const enabled = f.nativeElement.querySelector('[formControlName="rds_rt_enabled"]');
    const text = f.nativeElement.querySelector('[formControlName="rds_rt"]');
    expect(enabled.compareDocumentPosition(text) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(
      [...f.nativeElement.querySelectorAll('h3')].some(
        (h: any) => h.textContent === 'Radio Text (RT)',
      ),
    ).toBe(true);
    c.form.controls.rds_rt_enabled.setValue(false);
    f.changeDetectorRef.markForCheck();
    f.detectChanges();
    const pending = c.save();
    const req = http.expectOne('/api/v1/modulators/0/save-and-resume');
    expect(req.request.body.rds_override_profile).toMatchObject({
      rds_rt_enabled: false,
      rds_rt_termination: 'PAD',
      rds_rt: 'Keep text',
      rds_metadata_to_rt: true,
    });
    const configuration = { ...sample().modulators[0], ...req.request.body };
    req.flush({
      saved: true,
      stop_confirmed: true,
      restarted: false,
      failed_stage: null,
      message: 'Configuration saved; TX remains stopped.',
      configuration,
    });
    await pending;
    c.load(configuration);
    f.detectChanges();
    expect(c.form.controls.rds_rt_enabled.value).toBe(false);
    expect(c.form.controls.rds_rt_termination.value).toBe('PAD');
    expect(c.form.controls.rds_rt.value).toBe('Keep text');
    expect(f.nativeElement.querySelector('.editor-feedback').textContent).toContain(
      'Configuration saved',
    );
  });
  it('protects Cancel/navigation and browser unload', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.form.markAsDirty();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    expect(c.canLeave()).toBe(false);
    confirm.mockReturnValue(true);
    expect(c.canLeave()).toBe(true);
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    c.cancel();
    expect(navigate).toHaveBeenCalledWith(['/modulators']);
    const event = new Event('beforeunload', { cancelable: true }) as BeforeUnloadEvent;
    c.beforeUnload(event);
    expect(event.defaultPrevented).toBe(true);
    c.form.markAsPristine();
    expect(c.canLeave()).toBe(true);
    expect(modulatorDirtyGuard).toBeDefined();
  });
  it('shows load failure without an editable fake configuration', async () => {
    const f = TestBed.createComponent(ModulatorEditor);
    f.detectChanges();
    http.expectOne('/api/v1/modulators/0').error(new ProgressEvent('error'));
    await f.whenStable();
    f.detectChanges();
    expect(f.nativeElement.querySelector('form')).toBeNull();
    expect(f.componentInstance.failed()).toBe(true);
  });
  it('clears source on ANALOG and never restores a hidden digital assignment', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.form.controls.audio_mode.setValue('ANALOG');
    c.changeMode();
    f.detectChanges();
    expect(c.draft().input_source_id).toBeNull();
    expect(f.nativeElement.querySelector('[formControlName="input_source_id"]')).toBeNull();
    c.form.controls.audio_mode.setValue('DIGITAL');
    c.changeMode();
    expect(c.form.controls.input_source_id.value).toBeNull();
    expect(() => c.draft()).toThrow('Select a source for FMheadend audio');
    c.form.controls.input_source_id.setValue(1);
    expect(c.draft().input_source_id).toBe(1); // Existing TXs may share it.
  });
  it.each([
    ['DIGITAL', 'AUTO'],
    ['DIGITAL', 'MANUAL'],
    ['ANALOG', 'AUTO'],
    ['ANALOG', 'MANUAL'],
  ] as const)('keeps Audio controls compact for %s / %s', async (audio_mode, gain_mode) => {
    const f = await editor({ audio_mode, gain_mode });
    const el = f.nativeElement as HTMLElement;
    const input = el.querySelector('[formControlName="audio_mode"]') as HTMLSelectElement;
    expect(Array.from(input.options).map((o) => o.textContent?.trim())).toEqual([
      'FMheadend audio',
      'External line input',
    ]);
    expect(el.querySelector('[formControlName="input_source_id"]') !== null).toBe(
      audio_mode === 'DIGITAL',
    );
    expect(el.querySelector('[formControlName="digital_trim_db"]')).toBeNull();
    const analog = el.querySelector(
      '[formControlName="analog_gain_db"]',
    ) as HTMLSelectElement | null;
    expect(analog !== null).toBe(audio_mode === 'ANALOG');
    if (analog) {
      expect(analog.disabled).toBe(gain_mode === 'AUTO');
      expect(el.textContent).toContain("Uses TX 0's dedicated analog audio input.");
    }
    const clipping = el.querySelector('[formControlName="soft_clipping"]') as HTMLSelectElement;
    expect(clipping.disabled).toBe(false);
    expect(clipping.closest('label')?.classList.contains('full-width')).toBe(
      audio_mode === 'ANALOG',
    );
  });
  it('retains manual values under AUTO and soft clipping stays independent', async () => {
    const f = await editor({
        audio_mode: 'ANALOG',
        input_source_id: null,
        analog_gain_db: 6,
      }),
      c = f.componentInstance;
    expect(c.form.controls.analog_gain_db.disabled).toBe(true);
    expect(f.nativeElement.querySelector('[formControlName="analog_gain_db"]')).not.toBeNull();
    c.form.controls.soft_clipping.setValue(true);
    expect(c.draft()).toMatchObject({ analog_gain_db: 6, soft_clipping: true });
    c.form.controls.gain_mode.setValue('MANUAL');
    c.syncGain();
    expect(c.form.controls.analog_gain_db.enabled).toBe(true);
    c.form.controls.audio_mode.setValue('DIGITAL');
    c.changeMode();
    f.detectChanges();
    expect(f.nativeElement.querySelector('[formControlName="analog_gain_db"]')).toBeNull();
    expect(f.nativeElement.querySelector('[formControlName="digital_trim_db"]')).toBeNull();
    c.form.controls.input_source_id.setValue(1);
    expect(c.draft()).not.toHaveProperty('digital_trim_db');
  });
  it.each([true, false, null])(
    'displays current analog saturation %s honestly',
    async (saturated) => {
      const f = await editor({ audio_mode: 'ANALOG', input_source_id: null });
      const data = sample();
      data.modulators[0].audio_mode = 'ANALOG';
      data.modulators[0].input_source_id = null;
      data.status.modulators[0].input_saturated = saturated;
      shared.channels.set(compose(data));
      f.detectChanges();
      expect(f.nativeElement.textContent.includes('saturation detected')).toBe(saturated === true);
      expect(f.nativeElement.textContent.includes('No current input saturation')).toBe(
        saturated === false,
      );
      data.modulators[0].audio_mode = 'DIGITAL';
      shared.channels.set(compose(data));
      f.detectChanges();
      expect(f.nativeElement.textContent).not.toContain('saturation detected');
    },
  );
  it('preserves RDS values when disabled and exposes only normal PTY choices', async () => {
    const f = await editor({ rds_pty: 31 }),
      c = f.componentInstance;
    expect(ptyLabels).toHaveLength(30);
    expect(c.draft().rds_override_profile!.rds_pty).toBe(31);
    const options = Array.from(
      f.nativeElement.querySelector('[formControlName="rds_pty"]').options,
    ) as HTMLOptionElement[];
    expect(options.filter((o) => !o.disabled)).toHaveLength(30);
    c.form.controls.rds_enabled.setValue(false);
    const body = c.draft();
    expect(body.rds_override_profile!.rds_ps).toEqual(sample().modulators[0].rds_ps);
    expect(body.rds_override_profile!.rds_pty).toBe(31);
    c.form.controls.rds_pty.setValue(10);
    expect(c.draft().rds_override_profile!.rds_pty).toBe(10);
  });
  it('edits PS entries, duration, order and static/carousel semantics', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.addPs();
    c.ps.at(1).patchValue({ text: 'SECOND', duration_s: '7.5' });
    c.movePs(1, -1);
    expect(c.draft().rds_override_profile!.rds_ps[0]).toEqual({ text: 'SECOND', duration_s: 7.5 });
    c.removePs(1);
    expect(c.ps.length).toBe(1);
    c.removePs(0);
    expect(c.ps.length).toBe(1);
    c.ps.at(0).controls.text.setValue('TOO LONG TEXT');
    expect(() => c.draft()).toThrow('8 printable');
  });
  it('has no Modulator Fill from source action', async () => {
    const f = await editor();
    expect(f.nativeElement.textContent).not.toContain('Fill from source');
    expect(f.nativeElement.textContent).not.toContain('Fill PI');
  });
  it('edits AF exactly and rejects duplicate/raster/size mistakes', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.addAf();
    c.af.at(0).setValue('100.7');
    expect(c.draft().rds_override_profile!.rds_af_khz).toEqual([100700]);
    c.addAf();
    c.af.at(1).setValue('100.7');
    expect(() => c.draft()).toThrow('duplicate');
    c.af.at(1).setValue('87.55');
    expect(() => c.draft()).toThrow('AF:');
    c.removeAf(1);
    c.removeAf(0);
    expect(c.draft().rds_override_profile!.rds_af_khz).toEqual([]);
    for (let i = 0; i < 26; i++) c.addAf();
    expect(c.af.length).toBe(25);
  });
  it('reloads a reused editor route and ignores an older outstanding response', async () => {
    const changes = new Subject<ReturnType<typeof convertToParamMap>>();
    routeStub.paramMap = changes.asObservable();
    const f = TestBed.createComponent(ModulatorEditor);
    f.detectChanges();
    changes.next(convertToParamMap({ id: '0' }));
    const old = http.expectOne('/api/v1/modulators/0');
    changes.next(convertToParamMap({ id: '1' }));
    http.expectOne('/api/v1/modulators/1').flush(sample().modulators[1]);
    old.flush(sample().modulators[0]);
    await f.whenStable();
    f.detectChanges();
    expect(f.componentInstance.id).toBe(1);
    expect(f.componentInstance.form.controls.frequency_khz.value).toBe(
      sample().modulators[1].frequency_khz,
    );
    expect(f.nativeElement.querySelector('h1').textContent).toBe('TX 1');
  });
  it('background refresh never replaces dirty form fields', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.form.controls.rds_rt.setValue('Unsaved');
    c.form.markAsDirty();
    shared.data.set(sample());
    shared.channels.set(compose(sample()));
    f.detectChanges();
    expect(c.form.controls.rds_rt.value).toBe('Unsaved');
  });
});
