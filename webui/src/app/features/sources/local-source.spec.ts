import { TestBed } from '@angular/core/testing';
import { provideRouter, ActivatedRoute, convertToParamMap, Router } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { SourceEditor } from './source-editor';
import { LocalSourceConfig } from './source-api';
import { DashboardState } from '../dashboard/dashboard-state';
import { sample } from '../dashboard/test-fixtures';

describe('Local media source configuration', () => {
  let http: HttpTestingController;
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
    vi.spyOn(TestBed.inject(DashboardState), 'refreshAfterMutation').mockResolvedValue();
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  });
  afterEach(() => http.verify());
  function editor(saved?: LocalSourceConfig) {
    if (saved)
      Object.assign(TestBed.inject(ActivatedRoute).snapshot, {
        paramMap: convertToParamMap({ id: '1' }),
      });
    const f = TestBed.createComponent(SourceEditor);
    f.detectChanges();
    http.expectOne('/api/v1/audio-profiles/files').flush([]);
    if (saved)
      http
        .expectOne('/api/v1/sources/1')
        .flush({ ...sample().sources[0], type: 'LOCAL', config: saved });
    else {
      const select: HTMLSelectElement = f.nativeElement.querySelector('[formControlName="type"]');
      expect(select.querySelector('option[value="LOCAL"]')?.textContent).toContain('Local media');
      select.value = 'LOCAL';
      select.dispatchEvent(new Event('change'));
      f.componentInstance.form.controls.name.setValue('Music');
      f.detectChanges();
    }
    return f;
  }
  it('keeps one editable Server path input beside Browse without manual-edit controls', () => {
    const f = editor(),
      c = f.componentInstance;
    const browse = [...f.nativeElement.querySelectorAll('button')].find(
      (button: HTMLButtonElement) => button.textContent?.trim() === 'Browse...',
    ) as HTMLButtonElement;
    expect(browse.classList.contains('path-browse')).toBe(true);
    const controls = f.nativeElement.querySelector('.local-path-control-group') as HTMLElement;
    expect(controls.firstElementChild).toBe(browse);
    const path = controls.querySelector('#source-path') as HTMLInputElement;
    expect(path).toBeTruthy();
    expect(f.nativeElement.querySelectorAll('.local-path-panel input[type="text"]')).toHaveLength(
      1,
    );
    expect(path.disabled).toBe(false);
    expect(path.readOnly).toBe(false);
    expect(f.nativeElement.textContent).not.toContain('Edit manually');
    expect(f.nativeElement.textContent).not.toContain('Hide manual editing');
    path.value = '/custom/media';
    path.dispatchEvent(new Event('input'));
    expect(c.config.controls['path'].value).toBe('/custom/media');
    f.detectChanges();
    expect(path.title).toBe('/custom/media');
  });
  it('keeps Recursive with Folder path controls and hides it for Playlist mode', () => {
    const f = editor(),
      c = f.componentInstance;
    expect(c.config.getRawValue()).toEqual({
      mode: 'folder',
      path: '',
      order: 'sequential',
      recursive: false,
    });
    c.config.controls['path'].setValue('/mnt/music-link//album/../ music');
    const checkbox: HTMLInputElement = f.nativeElement.querySelector('#source-recursive');
    expect(checkbox.closest('.local-path-panel')).not.toBeNull();
    expect(checkbox.closest('.local-path-control-group')).not.toBeNull();
    expect(
      (f.nativeElement.querySelector('#source-order') as HTMLElement).closest('.local-path-panel'),
    ).toBeNull();
    checkbox.click();
    expect(c.config.controls['recursive'].value).toBe(true);
    const mode: HTMLSelectElement = f.nativeElement.querySelector('#source-mode');
    mode.value = 'playlist';
    mode.dispatchEvent(new Event('change'));
    f.detectChanges();
    expect(f.nativeElement.querySelector('#source-recursive')).toBeNull();
    expect(c.draft()?.config).toEqual({
      mode: 'playlist',
      path: '/mnt/music-link//album/../ music',
      order: 'sequential',
      recursive: false,
    });
    mode.value = 'folder';
    mode.dispatchEvent(new Event('change'));
    f.detectChanges();
    expect(f.nativeElement.querySelector('#source-recursive').checked).toBe(false);
    expect(c.config.controls['path'].value).toBe('/mnt/music-link//album/../ music');
    expect(c.dirty()).toBe(true);
  });
  it('renders long editable paths with their complete title', () => {
    const f = editor();
    const path = '/mnt/nas/ZENE/2026/2026 10 October/Some Very Long Directory Name/Favorites';
    f.componentInstance.config.controls['path'].setValue(path);
    f.detectChanges();
    const input = f.nativeElement.querySelector('#source-path') as HTMLInputElement;
    expect(input.value).toBe(path);
    expect(input.title).toBe(path);
  });
  const configs: LocalSourceConfig[] = [
    { mode: 'folder', path: '/mnt/music', order: 'sequential', recursive: false },
    { mode: 'folder', path: '/mnt/nas/offline/music', order: 'random', recursive: true },
    { mode: 'playlist', path: '/srv/fmheadend/list.m3u', order: 'random', recursive: false },
  ];
  it.each(configs)(
    'creates $mode / $order through the generic API without checking availability',
    async (config) => {
      const c = editor().componentInstance;
      c.config.setValue({ ...config });
      const pending = c.save();
      const request = http.expectOne('/api/v1/sources');
      expect(request.request.method).toBe('POST');
      expect(request.request.body.type).toBe('LOCAL');
      expect(request.request.body.config).toEqual(config);
      request.flush({ ...sample().sources[0], ...request.request.body });
      await pending;
      http.expectNone('/api/v1/sources/test');
    },
  );
  it.each(configs)('loads and updates $mode / $order without rewriting paths', async (config) => {
    const original = { ...config, path: config.path + ' space é//../link ' };
    const f = editor(original);
    await f.whenStable();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.config.getRawValue()).toEqual(original);
    expect(!!f.nativeElement.querySelector('#source-recursive')).toBe(config.mode === 'folder');
    c.config.controls['order'].setValue(config.order === 'random' ? 'sequential' : 'random');
    const pending = c.save();
    const request = http.expectOne('/api/v1/sources/1');
    expect(request.request.method).toBe('PATCH');
    expect(request.request.body.config).toEqual({
      ...original,
      order: c.config.controls['order'].value,
    });
    request.flush({ ...sample().sources[0], ...request.request.body });
    await pending;
  });
  it('normalizes ignored recursive on a saved playlist without affecting the path', async () => {
    const f = editor({
      mode: 'playlist',
      path: '/srv/old.m3u',
      order: 'sequential',
      recursive: true,
    });
    await f.whenStable();
    f.detectChanges();
    expect(f.nativeElement.querySelector('#source-recursive')).toBeNull();
    expect(f.componentInstance.draft()?.config).toEqual({
      mode: 'playlist',
      path: '/srv/old.m3u',
      order: 'sequential',
      recursive: false,
    });
  });
  it.each(['', '   ', 'relative/music', 'file:///mnt/music', '/mnt/\0bad'])(
    'rejects malformed path %s without requests',
    async (path) => {
      const c = editor().componentInstance;
      c.config.controls['path'].setValue(path);
      await c.save();
      expect(c.error()).toBeTruthy();
      http.expectNone('/api/v1/sources');
    },
  );
  it('uses normal type-switch reset behavior', () => {
    const c = editor().componentInstance;
    c.config.controls['path'].setValue('/mnt/music');
    c.form.controls.type.setValue('HTTP');
    c.changeType();
    c.config.controls['uri'].setValue('https://example.org/live');
    expect(c.draft()?.config).toEqual({ uri: 'https://example.org/live' });
    c.form.controls.type.setValue('LOCAL');
    c.changeType();
    expect(c.config.getRawValue()).toEqual({
      mode: 'folder',
      path: '',
      order: 'sequential',
      recursive: false,
    });
  });
});
