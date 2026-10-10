import { rdsText } from '../modulators/values';
import { RdsProfileForm } from '../../shared/rds-profile-editor/rds-profile-form';
import { RdsProfileEditor } from '../../shared/rds-profile-editor/rds-profile-editor';
import {
  Component,
  ElementRef,
  HostListener,
  OnInit,
  ViewChild,
  computed,
  inject,
  signal,
} from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, CanDeactivateFn, Router, RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import {
  SourceApi,
  SourceDraft,
  SourceType,
  SourceTest,
  Profile,
  Json,
  LocalSourceConfig,
  FilesystemBrowse,
  FilesystemLocation,
} from './source-api';
import { fields } from './source-fields';
import {
  PODCAST_MAX_FEEDS,
  PodcastForm,
  ScheduleMode,
  buildPodcastForm,
  controlError,
  FEED_TITLE_UNAVAILABLE,
  feedStatus,
  feedErrorText,
  feedsError,
  newFeedGroup,
  podcastConfig,
  scheduleDescriptions,
} from './podcast-form';
import { TestResult } from './test-result';
import { errorMessage } from '../../core/api/headend-api';
import { DashboardState } from '../dashboard/dashboard-state';
@Component({
  host: { class: 'page-content editor-narrow' },
  selector: 'app-source-editor',
  imports: [ReactiveFormsModule, RouterLink, TestResult, RdsProfileEditor],
  templateUrl: './source-editor.html',
  styleUrl: './sources.css',
})
export class SourceEditor implements OnInit {
  @ViewChild('pathPicker') pathPicker?: ElementRef<HTMLDialogElement>;
  private api = inject(SourceApi);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private shared = inject(DashboardState);
  readonly id = this.route.snapshot.paramMap.has('id')
    ? Number(this.route.snapshot.paramMap.get('id'))
    : undefined;
  readonly types: SourceType[] = ['HTTP', 'UDP', 'RTP', 'ALSA', 'LOCAL', 'PODCAST'];
  readonly typeLabels: Record<SourceType, string> = {
    HTTP: 'HTTP',
    UDP: 'UDP',
    RTP: 'RTP',
    ALSA: 'Sound card (ALSA)',
    LOCAL: 'Local media',
    PODCAST: 'Podcast',
  };
  readonly fields = fields;
  readonly maxFeeds = PODCAST_MAX_FEEDS;
  readonly scheduleDescriptions = scheduleDescriptions;
  readonly controlError = controlError;
  readonly feedsError = feedsError;
  readonly testedFeeds = signal<string[]>([]);
  // Matches by tested URL (unique per source) so edits, adds and removes never show stale diagnostics.
  feedResult(index: number): { label: string; ok: boolean; status: string } | null {
    const probe = this.result()?.podcast;
    const url = String(this.feeds.at(index)?.controls.url.value ?? '').trim();
    const at = this.testedFeeds().indexOf(url);
    const feed = at >= 0 ? probe?.feeds[at] : undefined;
    if (!feed) return null;
    return feed.error
      ? {
          label: feed.title || FEED_TITLE_UNAVAILABLE,
          ok: false,
          status: `Error: ${feedErrorText(feed.error)}`,
        }
      : {
          label: feed.title || FEED_TITLE_UNAVAILABLE,
          ok: true,
          status: feedStatus(feed),
        };
  }
  readonly profiles = signal<Profile[]>([]);
  // Default profiles first, then custom; each group sorted by name. Labels are display-only.
  readonly profileOptions = computed(() => {
    const byName = (a: Profile, b: Profile) =>
      a.name.localeCompare(b.name, 'en', { sensitivity: 'base' }) ||
      a.name.localeCompare(b.name, 'en') ||
      a.path.localeCompare(b.path, 'en');
    return (['default', 'custom'] as const).flatMap((kind) =>
      this.profiles()
        .filter((profile) => profile.kind === kind)
        .sort(byName)
        .map((profile) => ({ path: profile.path, label: `[${kind}] ${profile.name}` })),
    );
  });
  readonly profileError = signal('');
  readonly loading = signal(false);
  readonly loadFailed = signal(false);
  readonly saving = signal(false);
  readonly testing = signal(false);
  readonly error = signal('');
  readonly result = signal<SourceTest | null>(null);
  readonly browserOpen = signal(false);
  readonly browserLoading = signal(false);
  readonly browserError = signal('');
  readonly browserLocations = signal<FilesystemLocation[]>([]);
  readonly browser = signal<FilesystemBrowse | null>(null);
  readonly selectedPlaylist = signal<string | null>(null);
  readonly rds = new RdsProfileForm();
  readonly configureRds = new FormControl(false, { nonNullable: true });
  private persistedRds = false;
  fillFromSource() {
    const name = this.form.controls.name.value.trim();
    if (!this.configureRds.value || !name || this.saving() || this.testing()) return;
    if (
      this.rds.ps.length > 1 &&
      !window.confirm('Replace all PS entries with one entry from the source name?')
    )
      return;
    this.rds.ps.clear();
    this.rds.addPs();
    this.rds.ps.at(0).controls.text.setValue(rdsText(name, 8));
    this.rds.form.controls.rds_rt.setValue(rdsText(name, 64));
    this.rds.form.markAsDirty();
  }
  toggleRds() {
    this.configureRds.markAsDirty();
  }
  readonly form = new FormGroup({
    name: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(200), Validators.pattern(/\S/)],
    }),
    type: new FormControl<SourceType>('HTTP', { nonNullable: true }),
    processor_mode: new FormControl<SourceDraft['processor_mode']>('bypass', { nonNullable: true }),
    input_gain_db: new FormControl(0, {
      nonNullable: true,
      validators: [Validators.required, Validators.max(20)],
    }),
    audio_profile_path: new FormControl<string | null>(null),
    gst_options: new FormControl('', { nonNullable: true }),
  });
  config = new FormGroup<Record<string, FormControl<string | boolean>>>({});
  podcast: PodcastForm = buildPodcastForm();
  get feeds() {
    return this.podcast.controls.feeds;
  }
  scheduleMode(): ScheduleMode {
    return this.podcast.controls.schedule_mode.value;
  }
  addFeed() {
    if (this.feeds.length >= PODCAST_MAX_FEEDS) return;
    this.feeds.push(newFeedGroup());
    this.feeds.markAsDirty();
  }
  removeFeed(index: number) {
    if (this.feeds.length <= 1) return;
    this.feeds.removeAt(index);
    this.feeds.markAsDirty();
  }
  constructor() {
    this.rebuild('HTTP');
  }
  async ngOnInit() {
    void firstValueFrom(this.api.profiles())
      .then((p) => this.profiles.set(p))
      .catch((e) => this.profileError.set(errorMessage(e)));
    if (this.id === undefined) return;
    this.loading.set(true);
    try {
      const source = await firstValueFrom(this.api.get(this.id));
      this.form.setValue({
        name: source.name,
        type: source.type,
        audio_profile_path: source.audio_profile_path,
        processor_mode: source.processor_mode,
        input_gain_db: source.input_gain_db,
        gst_options: Object.keys(source.gst_options).length
          ? JSON.stringify(source.gst_options, null, 2)
          : '',
      });
      this.persistedRds = !!source.rds_profile;
      this.configureRds.setValue(this.persistedRds);
      if (source.rds_profile) this.rds.load(source.rds_profile);
      this.rebuild(source.type, source.config);
      this.form.markAsPristine();
    } catch (e) {
      this.error.set(errorMessage(e));
      this.loadFailed.set(true);
    } finally {
      this.loading.set(false);
    }
  }
  rebuild(type: SourceType, values: Record<string, unknown> = {}) {
    const controls: Record<string, FormControl<string | boolean>> = {};
    for (const f of fields[type])
      controls[f.key] = new FormControl<string | boolean>(
        f.checkbox ? values[f.key] === true : String(values[f.key] ?? f.defaultValue ?? ''),
        {
          nonNullable: true,
          validators: f.required ? [Validators.required, Validators.pattern(/\S/)] : [],
        },
      );
    this.config = new FormGroup(controls);
    this.podcast = buildPodcastForm(type === 'PODCAST' ? values : {});
    this.testedFeeds.set([]);
  }
  visibleFields() {
    return fields[this.form.controls.type.value].filter(
      (f) => f.key !== 'recursive' || this.config.controls['mode']?.value === 'folder',
    );
  }
  changeConfig(key: string) {
    if (
      this.form.controls.type.value === 'LOCAL' &&
      key === 'mode' &&
      this.config.controls['mode'].value === 'playlist'
    )
      this.config.controls['recursive'].setValue(false);
    this.result.set(null);
  }
  changeType() {
    this.rebuild(this.form.controls.type.value);
    this.config.markAsDirty();
    this.form.controls.gst_options.setValue('');
    this.result.set(null);
    this.closeBrowser();
  }
  changeProcessor() {
    this.form.controls.audio_profile_path.setValue(null);
    this.form.markAsDirty();
  }
  selectedProfile() {
    return this.profiles().find(
      (profile) => profile.path === this.form.controls.audio_profile_path.value,
    );
  }
  missingProfile() {
    const id = this.form.controls.audio_profile_path.value;
    return id !== null && !this.profiles().some((profile) => profile.path === id);
  }
  localMode(): LocalSourceConfig['mode'] {
    return this.config.controls['mode']?.value === 'playlist' ? 'playlist' : 'folder';
  }
  pickerTitle() {
    return this.localMode() === 'playlist' ? 'Select playlist' : 'Select media folder';
  }
  confirmLabel() {
    return this.localMode() === 'playlist' ? 'Select playlist' : 'Select folder';
  }
  async openBrowser() {
    if (this.browserLoading()) return;
    this.browserOpen.set(true);
    this.browserError.set('');
    this.browser.set(null);
    this.selectedPlaylist.set(null);
    const dialog = this.pathPicker?.nativeElement;
    if (dialog?.showModal) dialog.showModal();
    else dialog?.setAttribute('open', '');
    await this.loadLocations();
  }
  closeBrowser() {
    this.browserOpen.set(false);
    const dialog = this.pathPicker?.nativeElement;
    if (dialog?.open && dialog.close) dialog.close();
    else dialog?.removeAttribute('open');
  }
  async loadLocations() {
    if (this.browserLoading()) return;
    this.browserLoading.set(true);
    this.browserError.set('');
    try {
      this.browserLocations.set((await firstValueFrom(this.api.filesystemLocations())).locations);
      this.browser.set(null);
      this.selectedPlaylist.set(null);
    } catch (e) {
      this.browserError.set(errorMessage(e));
    } finally {
      this.browserLoading.set(false);
    }
  }
  async browsePath(path: string) {
    if (this.browserLoading()) return;
    this.browserLoading.set(true);
    this.browserError.set('');
    this.selectedPlaylist.set(null);
    try {
      this.browser.set(await firstValueFrom(this.api.browseFilesystem(path, this.localMode())));
    } catch (e) {
      this.browser.set(null);
      this.browserError.set(errorMessage(e));
    } finally {
      this.browserLoading.set(false);
    }
  }
  refreshBrowser() {
    const current = this.browser();
    return current ? this.browsePath(current.path) : this.loadLocations();
  }
  navigateEntry(name: string) {
    const current = this.browser();
    if (current) void this.browsePath(`${current.path.replace(/\/$/, '')}/${name}`);
  }
  goUp() {
    const current = this.browser();
    if (!current) return;
    if (!current.can_go_up) {
      void this.loadLocations();
      return;
    }
    const parent = current.path.slice(0, current.path.lastIndexOf('/')) || '/';
    void this.browsePath(parent);
  }
  breadcrumbs() {
    const current = this.browser();
    if (!current) return [];
    const rootParts = current.root.split('/').filter(Boolean);
    const pathParts = current.path.split('/').filter(Boolean);
    const breadcrumbs = [
      {
        name: rootParts.at(-1) ?? current.root,
        path: current.root,
        enabled: current.path !== current.root,
      },
    ];
    for (const name of pathParts.slice(rootParts.length)) {
      const path = `${breadcrumbs.at(-1)!.path}/${name}`;
      breadcrumbs.push({ name, path, enabled: path !== current.path });
    }
    return breadcrumbs;
  }
  selectPlaylist(path: string) {
    this.selectedPlaylist.set(path);
  }
  selectBrowserValue() {
    const value = this.localMode() === 'playlist' ? this.selectedPlaylist() : this.browser()?.path;
    if (!value) return;
    this.config.controls['path'].setValue(value);
    this.config.controls['path'].markAsDirty();
    this.closeBrowser();
  }
  dirty() {
    return (
      this.form.dirty ||
      this.config.dirty ||
      this.podcast.dirty ||
      this.configureRds.dirty ||
      this.rds.form.dirty
    );
  }
  canLeave() {
    return !this.saving() && (!this.dirty() || window.confirm('Discard unsaved Source changes?'));
  }
  @HostListener('window:beforeunload', ['$event']) beforeUnload(event: BeforeUnloadEvent) {
    if (this.dirty() || this.saving()) {
      event.preventDefault();
      event.returnValue = '';
    }
  }
  draft(forTest = false): SourceDraft | null {
    this.form.markAllAsTouched();
    this.config.markAllAsTouched();
    this.podcast.markAllAsTouched();
    this.error.set('');
    const blankHttpName =
      forTest && this.form.controls.type.value === 'HTTP' && !this.form.controls.name.value.trim();
    if (
      (this.form.invalid &&
        !(
          blankHttpName &&
          Object.entries(this.form.controls).every(
            ([key, control]) => key === 'name' || control.valid,
          )
        )) ||
      this.config.invalid ||
      (this.form.controls.type.value === 'PODCAST' && this.podcast.invalid)
    ) {
      this.error.set('Complete the required fields.');
      return null;
    }
    const v = this.form.getRawValue();
    if (v.processor_mode === 'stereotool') {
      this.error.set('Stereo Tool is not available yet.');
      return null;
    }
    if (v.processor_mode === 'builtin' && (!v.audio_profile_path || this.missingProfile())) {
      this.error.set('Select an available Built-in Audio Profile.');
      return null;
    }
    const config: Record<string, Json> = {};
    for (const f of fields[v.type]) {
      if (f.checkbox) {
        config[f.key] = this.config.controls[f.key].value === true;
        continue;
      }
      const raw = String(this.config.controls[f.key].value ?? '');
      if (!raw.trim()) continue;
      if (f.numeric) {
        const n = Number(raw);
        if (
          !Number.isSafeInteger(n) ||
          (f.min !== undefined && n < f.min) ||
          (f.max !== undefined && n > f.max)
        ) {
          this.error.set(`Check ${f.label}. Enter an integer within the indicated range.`);
          return null;
        }
        config[f.key] = n;
      } else config[f.key] = raw;
    }
    if (v.type === 'LOCAL') {
      const { mode, path, order } = config;
      if (
        (mode !== 'folder' && mode !== 'playlist') ||
        (order !== 'sequential' && order !== 'random')
      ) {
        this.error.set('Select a Local media mode and playback order.');
        return null;
      }
      // The appliance host is Linux; never normalize or resolve the saved path.
      if (typeof path !== 'string' || !path.startsWith('/') || path.includes('\0')) {
        this.error.set('Enter an absolute path on the FMHeadend host.');
        return null;
      }
      const local: LocalSourceConfig = {
        mode,
        path,
        order,
        recursive: mode === 'folder' && config['recursive'] === true,
      };
      Object.assign(config, local);
    }
    if (v.type === 'PODCAST') Object.assign(config, podcastConfig(this.podcast));
    if (v.type === 'HTTP') {
      try {
        const url = new URL(String(config['uri']));
        if (!['http:', 'https:'].includes(url.protocol) || /\s/.test(String(config['uri'])))
          throw new Error();
      } catch {
        this.error.set('Enter a valid HTTP or HTTPS stream URL.');
        return null;
      }
    }
    if (config['codec_data'] && !/^(?:[0-9a-fA-F]{2})+$/.test(String(config['codec_data']))) {
      this.error.set('Codec initialization data must contain complete hexadecimal byte pairs.');
      return null;
    }
    let options: SourceDraft['gst_options'] = {};
    try {
      const parsed: unknown = v.gst_options.trim() ? JSON.parse(v.gst_options) : {};
      if (
        !parsed ||
        typeof parsed !== 'object' ||
        Array.isArray(parsed) ||
        Object.entries(parsed).some(
          ([k, value]) =>
            !['source', 'queue', 'resample'].includes(k) ||
            !value ||
            typeof value !== 'object' ||
            Array.isArray(value),
        )
      )
        throw new Error();
      options = parsed as SourceDraft['gst_options'];
    } catch {
      this.error.set(
        'GStreamer options must be a JSON object containing source, queue or resample property objects.',
      );
      return null;
    }
    let rdsProfile = null;
    try {
      rdsProfile = this.configureRds.value ? this.rds.draft() : null;
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : 'Check RDS configuration.');
      return null;
    }
    return {
      rds_profile: rdsProfile,
      name: blankHttpName ? 'HTTP stream probe' : v.name,
      type: v.type,
      processor_mode: v.processor_mode,
      input_gain_db: v.input_gain_db,
      audio_profile_path: v.processor_mode === 'bypass' ? null : v.audio_profile_path,
      config,
      gst_options: options,
    };
  }
  async save() {
    if (this.saving() || this.testing() || this.loadFailed()) return;
    const body = this.draft();
    if (!body) return;
    this.saving.set(true);
    try {
      const result = await firstValueFrom(this.api.save(body, this.id));
      this.form.markAsPristine();
      this.config.markAsPristine();
      this.podcast.markAsPristine();
      this.configureRds.markAsPristine();
      this.rds.form.markAsPristine();
      this.persistedRds = !!result.rds_profile;
      await this.shared.refreshAfterMutation();
      this.saving.set(false);
      if (result.applied === false) {
        this.error.set('Source saved, but audio restart failed: ' + result.apply_error);
      } else {
        await this.router.navigate(['/sources']);
      }
    } catch (e) {
      if (this.persistedRds && body.rds_profile === null) this.configureRds.setValue(true);
      this.error.set(errorMessage(e));
    } finally {
      this.saving.set(false);
    }
  }
  async test() {
    if (this.testing() || this.saving() || this.loadFailed()) return;
    const body = this.draft(true);
    if (!body) return;
    this.testing.set(true);
    this.result.set(null);
    this.testedFeeds.set(
      body.type === 'PODCAST' ? (body.config['feeds'] as { url: string }[]).map((f) => f.url) : [],
    );
    try {
      const result = await firstValueFrom(this.api.test(body));
      this.result.set(result);
      if (
        body.type === 'HTTP' &&
        result.ok &&
        result.station_name?.trim() &&
        !this.form.controls.name.value.trim()
      ) {
        this.form.controls.name.setValue(result.station_name.trim());
        this.form.controls.name.markAsDirty();
      }
    } catch (e) {
      this.error.set(errorMessage(e));
    } finally {
      this.testing.set(false);
    }
  }
}
export const sourceDirtyGuard: CanDeactivateFn<SourceEditor> = (component) => component.canLeave();
