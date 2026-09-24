import { basicDefaults } from './basic-config';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { ProfilesList } from './profiles-list';
import { ProfileEditor } from './profile-editor';
import { AudioProfile, ProfileResult } from './profile-api';
const base = '/api/v1/audio-profiles/files';
const profile: ProfileResult = {
  name: 'Studio',
  path: 'custom/studio_123.json',
  kind: 'custom',
  profile: {
    schema_version: 1,
    name: 'Studio',
    stages: {
      gain: { selected: 'GST_VOLUME', implementations: { GST_VOLUME: { gain_db: -3 } } },
      processor: { selected: 'GST_BASIC', implementations: { GST_BASIC: { ...basicDefaults } } },
    },
  },
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
    expect(req.request.body.stages.processor.implementations.GST_BASIC).toEqual(basicDefaults);
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
  it('preserves inactive settings and disabled controls through save', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.basic.controls.eq_mid_db.setValue(-2.5);
    c.basic.controls.highpass_hz.setValue(80);
    c.basic.controls.highpass_enabled.setValue(false);
    c.form.controls.processor.setValue('NONE');
    c.form.controls.processor.setValue('GST_BASIC');
    expect(c.basic.controls.eq_mid_db.value).toBe(-2.5);
    expect(c.basic.controls.highpass_hz.disabled).toBe(true);
    c.form.controls.processor.setValue('NONE');
    const pending = c.save();
    const req = http.expectOne(base + '/' + profile.path);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body.stages.processor.selected).toBe('NONE');
    expect(req.request.body.stages.processor.implementations.GST_BASIC).toMatchObject({
      highpass_hz: 80,
      highpass_enabled: false,
      eq_mid_db: -2.5,
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
    http
      .expectOne(base + '/' + profile.path)
      .flush({
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
  it('keeps exact implemented filter/EQ/compressor controls', async () => {
    const f = await editor();
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
    expect(options('compressor_ratio')).toEqual(['1:1', '1.5:1', '2:1', '3:1', '4:1']);
    expect(options('compressor_threshold_dbfs')).toEqual(
      Array.from({ length: 28 }, (_, i) => `${i - 30} dBFS`),
    );
    const inputs = f.nativeElement.querySelectorAll(
      'section input[type="number"]',
    ) as NodeListOf<HTMLInputElement>;
    expect(inputs).toHaveLength(3);
    inputs.forEach((input) =>
      expect([input.min, input.max, input.step]).toEqual(['-24', '12', '0.5']),
    );
  });
});
