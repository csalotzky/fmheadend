import { RdsProfileForm } from '../../shared/rds-profile-editor/rds-profile-form';
import { RdsProfileEditor } from '../../shared/rds-profile-editor/rds-profile-editor';
import { SystemApi, FrequencyPlan, planOptions, frequencyLabel } from '../../core/api/system-api';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import {
  Component,
  DestroyRef,
  HostListener,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, CanDeactivateFn, Router } from '@angular/router';
import { firstValueFrom, forkJoin } from 'rxjs';
import { Modulator } from '../../core/api/models';
import { sortSources } from '../../core/api/source-order';
import { errorMessage } from '../../core/api/headend-api';
import { DashboardState } from '../dashboard/dashboard-state';
import { ModulatorApi, ModulatorDraft } from './modulator-api';
import { decimal, rfOutputOptions, analogGainOptions } from './values';
const text = (value = '') => new FormControl(value, { nonNullable: true });
const flag = (value = false) => new FormControl(value, { nonNullable: true });
@Component({
  host: { class: 'page-content editor-wide' },
  selector: 'app-modulator-editor',
  imports: [ReactiveFormsModule, RdsProfileEditor],
  templateUrl: './modulator-editor.html',
  styleUrl: './modulators.css',
})
export class ModulatorEditor implements OnInit {
  private api = inject(ModulatorApi);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  readonly shared = inject(DashboardState);
  private destroyRef = inject(DestroyRef);
  id = Number(this.route.snapshot.paramMap.get('id'));
  readonly loading = signal(true);
  readonly failed = signal(false);
  readonly saving = signal(false);
  readonly error = signal('');
  readonly message = signal('');
  private systemApi = inject(SystemApi);
  frequencies: { value: number; label: string }[] = [];
  plan: FrequencyPlan | null = null;
  storedFrequency: number | null = null;
  outsidePlan = false;
  readonly rfLevels = rfOutputOptions;
  readonly analogGains = analogGainOptions;
  readonly sources = computed(() => sortSources(this.shared.data()?.sources ?? []));
  readonly channel = computed(() => this.shared.channels().find((c) => c.id === this.id));
  readonly rds = new RdsProfileForm();
  readonly form = new FormGroup({
    ...this.rds.form.controls,
    rds_follow_source: flag(),
    frequency_khz: new FormControl<number | null>(null, Validators.required),
    rf_attenuation_db: new FormControl<number | null>(null, Validators.required),
    stereo: flag(true),
    preemphasis_us: new FormControl<50 | 75>(50, { nonNullable: true }),
    audio_mode: new FormControl<Modulator['audio_mode']>('DIGITAL', { nonNullable: true }),
    input_source_id: new FormControl<number | null>(null),
    gain_mode: new FormControl<Modulator['gain_mode']>('AUTO', { nonNullable: true }),
    analog_gain_db: text('0'),
    digital_trim_db: new FormControl<0 | 1>(0, { nonNullable: true }),
    soft_clipping: flag(),
    rds_enabled: flag(),
  });
  constructor() {
    this.rds.attach(this.form);
  }
  get ps() {
    return this.form.controls.rds_ps;
  }
  get af() {
    return this.form.controls.af;
  }
  ngOnInit() {
    this.route.paramMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      if (this.saving()) return;
      this.id = Number(params.get('id'));
      void this.loadConfig();
    });
  }
  async loadConfig() {
    if (this.saving()) return;
    const requestedId = this.id;
    this.loading.set(true);
    this.failed.set(false);
    this.error.set('');
    this.message.set('');
    if (!Number.isInteger(this.id) || this.id < 0 || this.id > 7) {
      this.failed.set(true);
      this.error.set('Select a physical TX from 0 to 7.');
      this.loading.set(false);
      return;
    }
    try {
      const { config, info } = await firstValueFrom(
        forkJoin({ config: this.api.get(requestedId), info: this.systemApi.info() }),
      );
      if (requestedId === this.id) {
        this.plan = info.frequency_plan;
        this.load(config);
      }
    } catch (e) {
      if (requestedId === this.id) {
        this.failed.set(true);
        this.error.set(errorMessage(e));
      }
    } finally {
      if (requestedId === this.id) this.loading.set(false);
    }
  }
  load(m: Modulator) {
    this.storedFrequency = m.frequency_khz;
    this.frequencies = this.plan ? planOptions(this.plan) : [];
    this.outsidePlan =
      m.frequency_khz !== null && !this.frequencies.some((o) => o.value === m.frequency_khz);
    if (this.outsidePlan)
      this.frequencies.unshift({
        value: m.frequency_khz!,
        label: frequencyLabel(m.frequency_khz!) + ' (stored, outside plan)',
      });
    this.form.patchValue({
      ...m,
      rds_ps: [],
      frequency_khz: m.frequency_khz,
      rf_attenuation_db: m.rf_attenuation_db,
      analog_gain_db: String(m.analog_gain_db),
    });
    this.syncGain();
    this.rds.load(m.rds_override_profile ?? m);
    this.form.markAsPristine();
  }
  changeMode() {
    if (this.form.controls.audio_mode.value === 'ANALOG') {
      this.form.controls.input_source_id.setValue(null);
      this.form.controls.rds_follow_source.setValue(false);
    }
    this.syncGain();
  }
  canFollowSource() {
    return (
      this.form.controls.audio_mode.value === 'DIGITAL' && !!this.selectedSource()?.rds_profile
    );
  }
  changeSource() {
    this.form.controls.rds_follow_source.setValue(this.canFollowSource());
  }
  syncGain() {
    const manual = this.form.controls.gain_mode.value === 'MANUAL';
    const controls = this.form.controls;
    controls.digital_trim_db[manual ? 'enable' : 'disable']({ emitEvent: false });
    controls.analog_gain_db[
      manual && controls.audio_mode.value === 'ANALOG' ? 'enable' : 'disable'
    ]({ emitEvent: false });
  }
  selectedSource() {
    return this.sources().find((s) => s.id === this.form.controls.input_source_id.value);
  }
  addPs() {
    this.rds.addPs();
    this.form.markAsDirty();
  }
  removePs(i: number) {
    this.rds.removePs(i);
    this.form.markAsDirty();
  }
  movePs(i: number, delta: number) {
    this.rds.movePs(i, delta);
    this.form.markAsDirty();
  }
  addAf() {
    this.rds.addAf();
    this.form.markAsDirty();
  }
  removeAf(i: number) {
    this.rds.removeAf(i);
    this.form.markAsDirty();
  }
  draft(): ModulatorDraft {
    const v = this.form.getRawValue();
    if (!this.frequencies.some((o) => o.value === v.frequency_khz))
      throw new Error('Select a frequency.');
    if (!rfOutputOptions.some((o) => o.value === v.rf_attenuation_db))
      throw new Error('Select RF output.');
    if (v.audio_mode === 'DIGITAL' && v.input_source_id === null)
      throw new Error('Select a digital source, or choose Analog input.');
    return {
      frequency_khz: v.frequency_khz,
      rf_attenuation_db: v.rf_attenuation_db,
      stereo: v.stereo,
      preemphasis_us: v.preemphasis_us,
      audio_mode: v.audio_mode,
      input_source_id: v.audio_mode === 'ANALOG' ? null : v.input_source_id,
      gain_mode: v.gain_mode,
      analog_gain_db: decimal(v.analog_gain_db, 'Analog gain', -3276.8, 3276.7)!,
      digital_trim_db: v.digital_trim_db,
      soft_clipping: v.soft_clipping,
      rds_enabled: v.rds_enabled,
      rds_follow_source: v.rds_follow_source,
      rds_override_profile: this.rds.draft(),
    };
  }
  async save() {
    if (this.saving() || this.loading() || this.failed()) return;
    this.error.set('');
    this.message.set('');
    let body: ModulatorDraft;
    try {
      body = this.draft();
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : 'Check the configuration.');
      return;
    }
    this.saving.set(true);
    try {
      const result = await firstValueFrom(this.api.saveAndResume(this.id, body));
      if (result.saved && result.configuration) this.load(result.configuration);
      if (result.failed_stage) this.error.set(result.message);
      else this.message.set(result.message);
      await this.shared.refreshAfterMutation();
    } catch (e) {
      this.error.set(errorMessage(e));
    } finally {
      this.saving.set(false);
    }
  }
  cancel() {
    void this.router.navigate(['/modulators']);
  }
  canLeave() {
    return (
      !this.saving() &&
      (!(this.form.dirty || this.rds.form.dirty) ||
        window.confirm('Discard unsaved Modulator changes?'))
    );
  }
  @HostListener('window:beforeunload', ['$event']) beforeUnload(event: BeforeUnloadEvent) {
    if (this.form.dirty || this.rds.form.dirty || this.saving()) {
      event.preventDefault();
      event.returnValue = '';
    }
  }
}
export const modulatorDirtyGuard: CanDeactivateFn<ModulatorEditor> = (c) => c.canLeave();
