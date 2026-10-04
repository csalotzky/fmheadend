import { TestBed } from '@angular/core/testing';
import { provideRouter, ActivatedRoute, convertToParamMap } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { SourceEditor } from './source-editor';
import { DashboardState } from '../dashboard/dashboard-state';

const locations = {
  locations: [
    { name: 'Mounted storage', path: '/mnt', available: true },
    { name: 'Removable media', path: '/media', available: false },
  ],
};
const root = {
  path: '/mnt',
  root: '/mnt',
  can_go_up: false,
  entries: [{ name: 'nas', type: 'directory' as const }],
};
const music = {
  path: '/mnt/nas/Music',
  root: '/mnt',
  can_go_up: true,
  entries: [
    { name: '2026', type: 'directory' as const },
    { name: 'favorites.M3U8', type: 'file' as const },
  ],
};

describe('LOCAL media path picker', () => {
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
  });
  afterEach(() => http.verify());
  function editor() {
    const fixture = TestBed.createComponent(SourceEditor);
    fixture.detectChanges();
    http.expectOne('/api/v1/audio-profiles/files').flush([]);
    const select: HTMLSelectElement = fixture.nativeElement.querySelector(
      '[formControlName="type"]',
    );
    select.value = 'LOCAL';
    select.dispatchEvent(new Event('change'));
    fixture.componentInstance.form.controls.name.setValue('Music');
    fixture.detectChanges();
    return fixture;
  }
  async function open(fixture: ReturnType<typeof editor>) {
    const button = [...fixture.nativeElement.querySelectorAll('button')].find(
      (element: HTMLButtonElement) => element.textContent?.trim() === 'Browse...',
    ) as HTMLButtonElement;
    expect(button).toBeTruthy();
    button.click();
    fixture.detectChanges();
    http.expectOne('/api/v1/filesystem/locations').flush(locations);
    await fixture.whenStable();
    fixture.detectChanges();
  }
  async function browse(
    fixture: ReturnType<typeof editor>,
    path: string,
    response: object,
    mode: 'folder' | 'playlist' = 'folder',
  ) {
    const request = http.expectOne(
      (candidate) =>
        candidate.url === '/api/v1/filesystem/browse' && candidate.params.get('path') === path,
    );
    expect(request.request.params.get('mode')).toBe(mode);
    request.flush(response);
    await fixture.whenStable();
    fixture.detectChanges();
  }

  it('opens from the editable Server path row and renders locations', async () => {
    const fixture = editor();
    const input = fixture.nativeElement.querySelector('#source-path') as HTMLInputElement;
    expect(input).toBeTruthy();
    input.value = '/mnt/manual';
    input.dispatchEvent(new Event('input'));
    await open(fixture);
    expect(fixture.nativeElement.querySelector('dialog')?.textContent).toContain(
      'Select media folder',
    );
    expect(fixture.nativeElement.textContent).toContain('Mounted storage');
    expect(fixture.nativeElement.textContent).toContain('Removable media');
    expect(input.value).toBe('/mnt/manual');
  });

  it('navigates locations, breadcrumbs, refreshes, and selects the current folder', async () => {
    const fixture = editor();
    await open(fixture);
    const rows = fixture.nativeElement.querySelectorAll('.picker-row');
    (rows[0] as HTMLButtonElement).click();
    await browse(fixture, '/mnt', root);
    expect(fixture.nativeElement.textContent).toContain('nas');
    const directory = fixture.nativeElement.querySelector('.picker-row') as HTMLButtonElement;
    expect(directory.querySelector('svg.picker-entry-icon.folder')).toBeTruthy();
    directory.click();
    await browse(fixture, '/mnt/nas', { ...music, path: '/mnt/nas' });
    const breadcrumb = [...fixture.nativeElement.querySelectorAll('.breadcrumb')].find(
      (element: HTMLButtonElement) => element.textContent?.trim() === 'mnt',
    ) as HTMLButtonElement;
    breadcrumb.click();
    await browse(fixture, '/mnt', root);
    const refresh = [...fixture.nativeElement.querySelectorAll('button')].find(
      (element: HTMLButtonElement) => element.textContent?.trim() === 'Refresh',
    ) as HTMLButtonElement;
    refresh.click();
    await browse(fixture, '/mnt', { ...root, entries: [{ name: 'new', type: 'directory' }] });
    expect(fixture.nativeElement.textContent).toContain('new');
    [...fixture.nativeElement.querySelectorAll('button')]
      .find((element: HTMLButtonElement) => element.textContent?.trim() === 'Select folder')!
      .click();
    expect(fixture.componentInstance.config.controls['path'].value).toBe('/mnt');
    expect(fixture.componentInstance.dirty()).toBe(true);
  });

  it('selects a playlist file without saving and Cancel preserves the existing path', async () => {
    const fixture = editor();
    fixture.componentInstance.config.controls['path'].setValue('/mnt/unchanged');
    fixture.componentInstance.config.controls['mode'].setValue('playlist');
    fixture.componentInstance.changeConfig('mode');
    fixture.detectChanges();
    await open(fixture);
    (fixture.nativeElement.querySelector('.picker-row') as HTMLButtonElement).click();
    await browse(fixture, '/mnt', { ...music, path: '/mnt' }, 'playlist');
    const playlist = [...fixture.nativeElement.querySelectorAll('.picker-row')].find(
      (element: HTMLButtonElement) => element.textContent?.includes('favorites.M3U8'),
    ) as HTMLButtonElement;
    expect(playlist.querySelector('svg.picker-entry-icon.playlist')).toBeTruthy();
    playlist.click();
    fixture.detectChanges();
    expect(playlist.classList.contains('selected')).toBe(true);
    [...fixture.nativeElement.querySelectorAll('button')]
      .find((element: HTMLButtonElement) => element.textContent?.trim() === 'Select playlist')!
      .click();
    expect(fixture.componentInstance.config.controls['path'].value).toBe('/mnt/favorites.M3U8');
    http.expectNone('/api/v1/sources');
    fixture.componentInstance.openBrowser();
    http.expectOne('/api/v1/filesystem/locations').flush(locations);
    await fixture.whenStable();
    fixture.detectChanges();
    [...fixture.nativeElement.querySelector('dialog')!.querySelectorAll('button')]
      .find((element: HTMLButtonElement) => element.textContent?.trim() === 'Cancel')!
      .click();
    expect(fixture.componentInstance.config.controls['path'].value).toBe('/mnt/favorites.M3U8');
  });

  it('shows unavailable-directory errors and recovers through Locations', async () => {
    const fixture = editor();
    await open(fixture);
    (fixture.nativeElement.querySelector('.picker-row') as HTMLButtonElement).click();
    const request = http.expectOne(
      (candidate) =>
        candidate.url === '/api/v1/filesystem/browse' && candidate.params.get('path') === '/mnt',
    );
    request.flush(
      { error: 'PATH_UNAVAILABLE', message: 'This location is no longer available.' },
      { status: 404, statusText: 'Not Found' },
    );
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('This location is no longer available.');
    [...fixture.nativeElement.querySelectorAll('button')]
      .find((element: HTMLButtonElement) => element.textContent?.trim() === 'Back to Locations')!
      .click();
    http.expectOne('/api/v1/filesystem/locations').flush(locations);
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Mounted storage');
  });
});
