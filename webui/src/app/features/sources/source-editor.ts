import { Component, HostListener, OnInit, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, CanDeactivateFn, Router, RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { SourceApi, SourceDraft, SourceType, SourceTest, Profile, Json } from './source-api';
import { fields } from './source-fields';
import { TestResult } from './test-result';
import { errorMessage } from '../../core/api/headend-api';
import { DashboardState } from '../dashboard/dashboard-state';
@Component({
  host: { class: 'page-content editor-narrow' },
  selector: 'app-source-editor',
  imports: [ReactiveFormsModule, RouterLink, TestResult],
  templateUrl: './source-editor.html',
  styleUrl: './sources.css',
})
export class SourceEditor implements OnInit {
  private api = inject(SourceApi);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private shared = inject(DashboardState);
  readonly id = this.route.snapshot.paramMap.has('id')
    ? Number(this.route.snapshot.paramMap.get('id'))
    : undefined;
  readonly types: SourceType[] = ['HTTP', 'UDP', 'RTP', 'ALSA'];
  readonly fields = fields;
  readonly profiles = signal<Profile[]>([]);
  readonly profileError = signal('');
  readonly loading = signal(false);
  readonly loadFailed = signal(false);
  readonly saving = signal(false);
  readonly testing = signal(false);
  readonly error = signal('');
  readonly result = signal<SourceTest | null>(null);
  readonly form = new FormGroup({
    name: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(200), Validators.pattern(/\S/)],
    }),
    type: new FormControl<SourceType>('HTTP', { nonNullable: true }),
    audio_profile_id: new FormControl<number | null>(null),
    gst_options: new FormControl('', { nonNullable: true }),
  });
  config = new FormGroup<Record<string, FormControl<string>>>({});
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
        audio_profile_id: source.audio_profile_id,
        gst_options: Object.keys(source.gst_options).length
          ? JSON.stringify(source.gst_options, null, 2)
          : '',
      });
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
    const controls: Record<string, FormControl<string>> = {};
    for (const f of fields[type])
      controls[f.key] = new FormControl(values[f.key] == null ? '' : String(values[f.key]), {
        nonNullable: true,
        validators: f.required ? [Validators.required, Validators.pattern(/\S/)] : [],
      });
    this.config = new FormGroup(controls);
  }
  changeType() {
    this.rebuild(this.form.controls.type.value);
    this.config.markAsDirty();
    this.form.controls.gst_options.setValue('');
    this.result.set(null);
  }
  missingProfile() {
    const id = this.form.controls.audio_profile_id.value;
    return id !== null && !this.profiles().some((profile) => profile.id === id);
  }
  dirty() {
    return this.form.dirty || this.config.dirty;
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
      this.config.invalid
    ) {
      this.error.set('Complete the required fields.');
      return null;
    }
    const v = this.form.getRawValue();
    const config: Record<string, Json> = {};
    for (const f of fields[v.type]) {
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
    return {
      name: blankHttpName ? 'HTTP stream probe' : v.name,
      type: v.type,
      audio_profile_id: v.audio_profile_id,
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
      await firstValueFrom(this.api.save(body, this.id));
      this.form.markAsPristine();
      this.config.markAsPristine();
      await this.shared.refreshAfterMutation();
      this.saving.set(false);
      await this.router.navigate(['/sources']);
    } catch (e) {
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
