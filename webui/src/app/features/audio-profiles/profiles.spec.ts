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
  async function editor(existing = false) {
    if (existing) route.snapshot.paramMap = convertToParamMap({ id: '1' });
    const f = TestBed.createComponent(ProfileEditor);
    f.detectChanges();
    if (existing) http.expectOne('/api/v1/audio-profiles/1').flush(profile);
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
    c.form.setValue({ name: 'New', gain_db: -6, processor });
    c.form.markAsDirty();
    const pending = c.save();
    const request = http.expectOne('/api/v1/audio-profiles');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ name: 'New', gain_db: -6, processor });
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
    expect(Object.keys(req.request.body).sort()).toEqual(['gain_db', 'name', 'processor']);
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
  it('shows Basic limitation and only implemented controls', async () => {
    const f = await editor();
    f.componentInstance.form.controls.processor.setValue('GST_BASIC');
    f.changeDetectorRef.markForCheck();
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('Additional Basic processing is not implemented');
    expect(f.nativeElement.querySelectorAll('input, select')).toHaveLength(3);
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
