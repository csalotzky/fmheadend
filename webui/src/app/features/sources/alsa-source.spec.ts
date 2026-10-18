import { TestBed } from '@angular/core/testing';
import { provideRouter, ActivatedRoute, convertToParamMap, Router } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { SourceEditor } from './source-editor';
import { DashboardState } from '../dashboard/dashboard-state';
import { sample } from '../dashboard/test-fixtures';

const endpoint = '/api/v1/alsa/capture-devices';
const device = {
  pcm: 'plughw:CARD=CODEC,DEV=0',
  label: 'USB Audio CODEC / USB Audio (CODEC, device 0)',
  card_id: 'CODEC',
  device: 0,
};
const probe = {
  ok: true,
  mocked: false,
  codec: 'PCM',
  sample_rate: 48000,
  channels: 2,
  output_caps: 'audio/x-raw',
  stream_title: null,
  message: 'Decoded PCM received',
};

describe('ALSA Source editor', () => {
  let http: HttpTestingController;
  beforeEach(() => {
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
    http = TestBed.inject(HttpTestingController);
    vi.spyOn(TestBed.inject(DashboardState), 'refreshAfterMutation').mockResolvedValue();
  });
  afterEach(() => http.verify());
  async function editor(pcm = device.pcm) {
    const f = TestBed.createComponent(SourceEditor);
    f.detectChanges();
    http.expectOne('/api/v1/audio-profiles/files').flush([]);
    http
      .expectOne('/api/v1/sources/1')
      .flush({
        ...sample().sources[0],
        type: 'ALSA',
        use_default_audio_profile: true,
        audio_profile_id: null,
        config: { device: pcm, channel_selection: 'stereo' },
      });
    // Let the saved Source construct its config and start discovery.
    await Promise.resolve();
    await Promise.resolve();
    http.expectOne(endpoint).flush({ devices: [device] });
    await f.whenStable();
    f.detectChanges();
    return f;
  }
  it('loads readable labels, technical name, channels, and no rate input', async () => {
    const f = await editor();
    const el: HTMLElement = f.nativeElement;
    expect(el.querySelector('#alsa-device-select')?.textContent).toContain(device.label);
    expect((el.querySelector('#alsa-device-select') as HTMLSelectElement).value).toBe(device.pcm);
    expect(el.textContent).toContain(`Selected: ${device.pcm}`);
    expect(el.querySelector('[formControlName="sample_rate"]')).toBeNull();
    expect(el.querySelector('[formControlName="channels"]')).toBeNull();
    expect(el.querySelector('#source-channel_selection')?.textContent).toContain('Left only');
    expect(f.componentInstance.dirty()).toBe(false);
  });
  it('refreshes without duplicates or dirtying and preserves unplugged/replugged selection', async () => {
    const f = await editor(),
      c = f.componentInstance;
    const pending = c.refreshAlsa();
    void c.refreshAlsa();
    f.detectChanges();
    expect(c.alsaLoading()).toBe(true);
    const button = [...(f.nativeElement as HTMLElement).querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Refreshing devices'),
    )!;
    expect(button.disabled).toBe(true);
    http.expectOne(endpoint).flush({ devices: [] });
    await pending;
    f.detectChanges();
    expect(c.config.controls['device'].value).toBe(device.pcm);
    expect(f.nativeElement.textContent).toContain('unavailable / not listed');
    expect(f.nativeElement.querySelector('#alsa-device-select').value).toBe(device.pcm);
    expect(f.nativeElement.textContent).toContain('No capture devices found');
    expect(c.dirty()).toBe(false);
    const again = c.refreshAlsa();
    http.expectOne(endpoint).flush({ devices: [device] });
    await again;
    f.detectChanges();
    expect(c.alsaMissing()).toBe(false);
    expect(f.nativeElement.querySelector('#alsa-device-select').value).toBe(device.pcm);
    expect(c.dirty()).toBe(false);
  });
  it('reports discovery errors without losing draft or last inventory', async () => {
    const f = await editor(),
      c = f.componentInstance;
    const pending = c.refreshAlsa();
    http
      .expectOne(endpoint)
      .flush(
        { message: 'Cannot enumerate ALSA capture devices' },
        { status: 503, statusText: 'Unavailable' },
      );
    await pending;
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('Cannot enumerate ALSA capture devices');
    expect(c.alsaDevices()).toEqual([device]);
    expect(c.draft()?.config['device']).toBe(device.pcm);
    expect(c.dirty()).toBe(false);
  });
  it('loads saved custom PCM and retains custom edits through refresh and selection changes', async () => {
    const f = await editor('my_dsnoop'),
      c = f.componentInstance;
    expect(c.alsaCustom()).toBe(true);
    const input = f.nativeElement.querySelector('[formControlName="device"]') as HTMLInputElement;
    expect(input.value).toBe('my_dsnoop');
    input.value = 'my_route';
    input.dispatchEvent(new Event('input'));
    expect(c.dirty()).toBe(true);
    const pending = c.refreshAlsa();
    http.expectOne(endpoint).flush({ devices: [] });
    await pending;
    expect(c.draft()?.config['device']).toBe('my_route');
    c.selectAlsa(device.pcm);
    c.selectAlsa('__custom');
    expect(c.config.controls['device'].value).toBe('my_route');
    http.expectNone('/api/v1/sources/1');
  });
  it.each(['stereo', 'mono', 'left', 'right'])(
    'saves %s and tests the current draft without navigating',
    async (mode) => {
      const f = await editor(),
        c = f.componentInstance;
      const navigate = vi.spyOn(TestBed.inject(Router), 'navigate');
      const select = f.nativeElement.querySelector(
        '#source-channel_selection',
      ) as HTMLSelectElement;
      select.value = mode;
      select.dispatchEvent(new Event('change'));
      expect(c.dirty()).toBe(true);
      const test = c.test();
      const request = http.expectOne('/api/v1/sources/test');
      expect(request.request.body.config).toEqual({ device: device.pcm, channel_selection: mode });
      request.flush(probe);
      await test;
      expect(c.result()?.ok).toBe(true);
      const save = c.save();
      const saved = http.expectOne('/api/v1/sources/1');
      expect(saved.request.method).toBe('PATCH');
      expect(saved.request.body.config).toEqual({ device: device.pcm, channel_selection: mode });
      saved.flush({ ...saved.request.body, id: 1, saved: true, applied: true });
      await save;
      expect(c.dirty()).toBe(false);
      expect(c.message()).toBe('Source saved.');
      expect(navigate).not.toHaveBeenCalled();
      f.detectChanges();
      expect(f.nativeElement.querySelector('button[routerLink="/sources"]').disabled).toBe(false);
    },
  );
  it('rejects empty/control-character custom names and preserves the unsaved guard', async () => {
    const f = await editor(),
      c = f.componentInstance;
    c.selectAlsa('__custom');
    c.config.controls['device'].setValue('');
    expect(c.draft()).toBeNull();
    c.config.controls['device'].setValue('bad\0name');
    expect(c.draft()).toBeNull();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    expect(c.canLeave()).toBe(false);
  });
});
