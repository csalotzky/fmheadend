import { basicDefaults } from './basic-config';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { ProfilesList } from './profiles-list';
import { ProfileEditor } from './profile-editor';
import { AudioProfile } from './profile-api';
const profile: AudioProfile = {
  id: 1,
  name: 'Studio',
  gain_db: -3,
  processor: 'NONE',
  timestamp: '2026-01-01',
  basic_config: { ...basicDefaults },
};
describe('Audio Profiles management', () => {
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
  async function list(profiles: AudioProfile[] = [profile]) {
    const f = TestBed.createComponent(ProfilesList);
    f.detectChanges();
    http.expectOne('/api/v1/audio-profiles').flush(profiles);
    http.expectOne('/api/v1/sources').flush([
      { id: 1, audio_profile_id: 1 },
      { id: 2, audio_profile_id: 1 },
    ]);
    await f.whenStable();
    f.detectChanges();
    return f;
  }
  async function editor(existing = false, saved = profile) {
    if (existing) route.snapshot.paramMap = convertToParamMap({ id: '1' });
    const f = TestBed.createComponent(ProfileEditor);
    f.detectChanges();
    if (existing) http.expectOne('/api/v1/audio-profiles/1').flush(saved);
    await f.whenStable();
    f.detectChanges();
    return f;
  }
  it('lists profiles, processing labels, gain, reference counts and edit', async () => {
    const f = await list([profile, { ...profile, id: 2, processor: 'GST_BASIC' }]);
    const el = f.nativeElement as HTMLElement;
    expect(el.classList.contains('page-content')).toBe(true);
    expect(el.querySelector('.page-header a')?.textContent).toContain('Add Audio Profile');
    expect(el.textContent).toContain('None (gain only)');
    expect(el.textContent).toContain('GStreamer Basic');
    expect(el.textContent).toContain('Gain: -3 dB');
    expect(el.textContent).toContain('Used by 2 sources');
    expect(el.querySelector('a[href="/audio-profiles/1"]')).not.toBeNull();
  });
  it('shows empty and unavailable states', async () => {
    const f = await list([]);
    expect(f.nativeElement.textContent).toContain('No audio profiles');
    const pending = f.componentInstance.load();
    http.expectOne('/api/v1/audio-profiles').flush({}, { status: 503, statusText: 'Unavailable' });
    const sources = http.match('/api/v1/sources');
    sources.forEach((r) => {
      if (!r.cancelled) r.flush([]);
    });
    await pending;
    f.detectChanges();
    expect(f.nativeElement.querySelector('[role="alert"]')).not.toBeNull();
  });
  it.each(['NONE', 'GST_BASIC'] as const)('creates %s with independent gain', async (processor) => {
    const f = await editor();
    const c = f.componentInstance;
    c.form.setValue({ name: 'New', gain_db: -6, processor, basic_config: { ...basicDefaults } });
    c.form.markAsDirty();
    const pending = c.save();
    const request = http.expectOne('/api/v1/audio-profiles');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      name: 'New',
      gain_db: -6,
      processor,
      ...(processor === 'GST_BASIC' ? { basic_config: basicDefaults } : {}),
    });
    request.flush({ ...profile, name: 'New', gain_db: -6, processor });
    await pending;
    expect(c.form.pristine).toBe(true);
    expect(c.canLeave()).toBe(true);
  });
  it('shows stop-before-save guidance only on existing profiles', async () => {
    const fresh = await editor();
    expect(fresh.nativeElement.textContent).not.toContain('Stop all transmitters');
    fresh.destroy();
    const existing = await editor(true);
    expect(existing.nativeElement.textContent).toContain(
      'Stop all transmitters using this profile before saving changes.',
    );
  });
  it('uses backend defaults on create and prevents duplicate saves', async () => {
    const f = await editor();
    const c = f.componentInstance;
    c.form.controls.name.setValue('Default');
    const pending = c.save();
    void c.save();
    const req = http.expectOne('/api/v1/audio-profiles');
    expect(req.request.body).toEqual({ name: 'Default' });
    req.flush(profile);
    await pending;
  });
  it('loads and edits without submitting reserved DSP fields', async () => {
    const f = await editor(true);
    const c = f.componentInstance;
    expect(c.form.controls.gain_db.value).toBe(-3);
    c.form.controls.name.setValue('Rename');
    const pending = c.save();
    const req = http.expectOne('/api/v1/audio-profiles/1');
    expect(req.request.method).toBe('PATCH');
    expect(Object.keys(req.request.body).sort()).toEqual([
      'basic_config',
      'gain_db',
      'name',
      'processor',
    ]);
    req.flush({ ...profile, name: 'Rename' });
    await pending;
  });
  it('preserves edits and displays an active-profile conflict without lifecycle calls', async () => {
    const f = await editor(true);
    const c = f.componentInstance;
    c.form.controls.gain_db.setValue(-9);
    c.form.markAsDirty();
    const pending = c.save();
    http.expectOne('/api/v1/audio-profiles/1').flush(
      {
        error: 'PROFILE_ACTIVE',
        message: 'Stop all transmitters using this profile before saving changes',
      },
      { status: 409, statusText: 'Conflict' },
    );
    await pending;
    f.detectChanges();
    expect(c.form.controls.gain_db.value).toBe(-9);
    expect(c.form.dirty).toBe(true);
    expect(f.nativeElement.textContent).toContain(
      'Stop all transmitters using this profile before saving changes',
    );
  });
  it('warns on dirty navigation, protects unload and cancels through router', async () => {
    const f = await editor();
    const c = f.componentInstance;
    c.form.markAsDirty();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    expect(c.canLeave()).toBe(false);
    const event = new Event('beforeunload', { cancelable: true });
    c.beforeUnload(event as BeforeUnloadEvent);
    expect(event.defaultPrevented).toBe(true);
    c.cancel();
    expect(TestBed.inject(Router).navigate).toHaveBeenCalledWith(['/audio-profiles']);
  });
  it('shows Basic only for GST_BASIC and retains edits across processor switches', async () => {
    const f = await editor();
    const c = f.componentInstance;
    expect(f.nativeElement.querySelector('#basic-heading')).toBeNull();
    c.form.controls.processor.setValue('GST_BASIC');
    f.changeDetectorRef.markForCheck();
    f.detectChanges();
    expect(f.nativeElement.querySelector('#basic-heading')?.textContent).toBe('Basic processing');
    c.basic.controls.eq_mid_db.setValue(-2.5);
    c.form.controls.processor.setValue('NONE');
    f.changeDetectorRef.markForCheck();
    f.detectChanges();
    expect(f.nativeElement.querySelector('#basic-heading')).toBeNull();
    c.form.controls.processor.setValue('GST_BASIC');
    f.changeDetectorRef.markForCheck();
    f.detectChanges();
    expect(c.basic.controls.eq_mid_db.value).toBe(-2.5);
  });
  it('loads API settings, preserves disabled fields, and serializes integer Hz and conventional ratio', async () => {
    const settings = {
      ...basicDefaults,
      highpass_hz: 80 as const,
      eq_low_db: -3.5,
      eq_mid_db: 2,
      eq_high_db: 0.5,
      compressor_threshold_dbfs: -18,
      compressor_ratio: 3 as const,
      lowpass_hz: 13000 as const,
    };
    const f = await editor(true, { ...profile, processor: 'GST_BASIC', basic_config: settings });
    const c = f.componentInstance;
    expect(c.basic.getRawValue()).toEqual(settings);
    c.basic.controls.highpass_enabled.setValue(false);
    c.basic.controls.compressor_enabled.setValue(false);
    c.basic.controls.lowpass_enabled.setValue(false);
    c.basic.controls.limiter_enabled.setValue(false);
    f.changeDetectorRef.markForCheck();
    f.detectChanges();
    for (const key of [
      'highpass_hz',
      'compressor_threshold_dbfs',
      'compressor_ratio',
      'lowpass_hz',
    ])
      expect(
        (f.nativeElement.querySelector(`[formControlName="${key}"]`) as HTMLSelectElement).disabled,
      ).toBe(true);
    expect(c.basic.getRawValue()).toEqual({
      ...settings,
      highpass_enabled: false,
      compressor_enabled: false,
      lowpass_enabled: false,
      limiter_enabled: false,
    });
    c.form.controls.processor.setValue('NONE');
    const pending = c.save();
    const request = http.expectOne('/api/v1/audio-profiles/1');
    expect(request.request.body.basic_config).toEqual(c.basic.getRawValue());
    expect(request.request.body.processor).toBe('NONE');
    request.flush(profile);
    await pending;
  });
  it('offers exact cutoff, threshold and ratio options and three half-dB EQ controls', async () => {
    const f = await editor(true, { ...profile, processor: 'GST_BASIC' });
    const c = f.componentInstance;
    const options = (key: string) =>
      Array.from(
        (f.nativeElement.querySelector(`[formControlName="${key}"]`) as HTMLSelectElement).options,
      ).map((o) => o.textContent?.trim());
    expect(options('highpass_hz')).toEqual([
      '20 Hz',
      '30 Hz',
      '40 Hz',
      '50 Hz',
      '60 Hz',
      '80 Hz',
      '100 Hz',
    ]);
    expect(options('lowpass_hz')).toEqual([
      '12.0 kHz',
      '13.0 kHz',
      '14.0 kHz',
      '15.0 kHz',
      '16.0 kHz',
    ]);
    expect(options('compressor_threshold_dbfs')).toEqual(
      Array.from({ length: 28 }, (_, i) => `${i - 30} dBFS`),
    );
    expect(options('compressor_ratio')).toEqual(['1:1', '1.5:1', '2:1', '3:1', '4:1']);
    const inputs = Array.from(
      (f.nativeElement as HTMLElement).querySelectorAll<HTMLInputElement>(
        'section input[type="number"]',
      ),
    );
    expect(inputs).toHaveLength(3);
    for (const input of inputs) {
      expect([input.min, input.max, input.step, input.value]).toEqual(['-24', '12', '0.5', '0']);
      input.value = '-1.5';
      input.dispatchEvent(new Event('input'));
    }
    expect(c.basic.getRawValue()).toMatchObject({
      eq_low_db: -1.5,
      eq_mid_db: -1.5,
      eq_high_db: -1.5,
    });
    for (const control of [
      c.basic.controls.highpass_enabled,
      c.basic.controls.compressor_enabled,
      c.basic.controls.lowpass_enabled,
    ]) {
      control.setValue(false);
      control.setValue(true);
    }
    expect(c.basic.controls.highpass_hz.enabled).toBe(true);
    expect(c.basic.controls.compressor_ratio.enabled).toBe(true);
    expect(c.basic.controls.compressor_threshold_dbfs.enabled).toBe(true);
    expect(c.basic.controls.lowpass_hz.enabled).toBe(true);
  });
  it('confirms deletion, reports reference conflict, and refreshes after deletion', async () => {
    const f = await list();
    const c = f.componentInstance;
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    await c.remove(profile);
    confirm.mockReturnValue(true);
    let pending = c.remove(profile);
    http
      .expectOne('/api/v1/audio-profiles/1')
      .flush({ message: 'Resource is referenced' }, { status: 409, statusText: 'Conflict' });
    await pending;
    expect(c.error()).toContain('Resource is referenced');
    pending = c.remove(profile);
    http.expectOne('/api/v1/audio-profiles/1').flush(null);
    await Promise.resolve();
    await Promise.resolve();
    http.expectOne('/api/v1/audio-profiles').flush([]);
    http.expectOne('/api/v1/sources').flush([]);
    await pending;
    expect(c.profiles()).toHaveLength(0);
  });
});
