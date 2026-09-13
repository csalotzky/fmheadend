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
import { FormArray, FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, CanDeactivateFn, Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { Modulator } from '../../core/api/models';
import { errorMessage } from '../../core/api/headend-api';
import { DashboardState } from '../dashboard/dashboard-state';
import { ModulatorApi, ModulatorDraft } from './modulator-api';
import {
  mhz,
  khz,
  hex,
  parseHex,
  ptyLabels,
  rdsText,
  checkText,
  decimal,
  frequencyOptions,
  rfOutputOptions,
  analogGainOptions,
  afOptions,
  textLength,
} from './values';
const text = (value = '') => new FormControl(value, { nonNullable: true });
const flag = (value = false) => new FormControl(value, { nonNullable: true });
const psEntry = (value = '', duration = '5') =>
  new FormGroup({ text: text(value), duration_s: text(duration) });
@Component({
  host: { class: 'page-content editor-wide' },
  selector: 'app-modulator-editor',
  imports: [ReactiveFormsModule],
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
  readonly labels = ptyLabels;
  readonly frequencies = frequencyOptions;
  readonly rfLevels = rfOutputOptions;
  readonly analogGains = analogGainOptions;
  readonly alternatives = afOptions;
  readonly mhz = mhz;
  readonly textLength = textLength;
  private storedPty = 0;
  readonly sources = computed(() => this.shared.data()?.sources ?? []);
  readonly channel = computed(() => this.shared.channels().find((c) => c.id === this.id));
  readonly form = new FormGroup({
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
    pi: text(),
    ecc: text(),
    rds_pty: new FormControl<number | null>(0),
    rds_tp: flag(),
    rds_ps: new FormArray([psEntry()]),
    af: new FormArray<FormControl<string>>([]),
    rds_rt: text(),
    rds_metadata_to_rt: flag(),
  });
  get ps() {
    return this.form.controls.rds_ps;
  }
  get af() {
    return this.form.controls.af;
  }
  ngOnInit() {
    this.route.paramMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      this.id = Number(params.get('id'));
      void this.loadConfig();
    });
  }
  async loadConfig() {
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
      const config = await firstValueFrom(this.api.get(requestedId));
      if (requestedId === this.id) this.load(config);
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
    this.storedPty = m.rds_pty;
    this.form.patchValue({
      ...m,
      rds_ps: [],
      rds_pty: m.rds_pty <= 29 ? m.rds_pty : null,
      frequency_khz: frequencyOptions.some((o) => o.value === m.frequency_khz)
        ? m.frequency_khz
        : null,
      rf_attenuation_db: m.rf_attenuation_db,
      analog_gain_db: String(m.analog_gain_db),
      pi: hex(m.rds_pi, 4),
      ecc: hex(m.rds_ecc, 2),
    });
    this.ps.clear();
    m.rds_ps.forEach((p) => this.ps.push(psEntry(p.text, String(p.duration_s))));
    this.af.clear();
    m.rds_af_khz.forEach((f) => this.af.push(text(mhz(f))));
    this.syncGain();
    this.form.markAsPristine();
  }
  changeMode() {
    if (this.form.controls.audio_mode.value === 'ANALOG')
      this.form.controls.input_source_id.setValue(null);
    this.syncGain();
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
    this.ps.push(psEntry());
    this.form.markAsDirty();
  }
  removePs(i: number) {
    if (this.ps.length > 1) {
      this.ps.removeAt(i);
      this.form.markAsDirty();
    }
  }
  movePs(i: number, delta: number) {
    const to = i + delta;
    if (to < 0 || to >= this.ps.length) return;
    const entry = this.ps.at(i);
    this.ps.removeAt(i);
    this.ps.insert(to, entry);
    this.form.markAsDirty();
  }
  addAf() {
    if (this.af.length < 25) {
      this.af.push(text());
      this.form.markAsDirty();
    }
  }
  removeAf(i: number) {
    this.af.removeAt(i);
    this.form.markAsDirty();
  }
  fillFromSource() {
    const name = this.selectedSource()?.name.trim();
    if (this.form.controls.audio_mode.value !== 'DIGITAL' || !name) return;
    if (
      this.ps.length > 1 &&
      !window.confirm('Replace all PS entries with one entry from the selected source?')
    )
      return;
    this.form.controls.pi.setValue(hex(0xff00 + this.id, 4));
    this.ps.clear();
    this.ps.push(psEntry(rdsText(name, 8)));
    this.form.controls.rds_rt.setValue(rdsText(name, 64));
    this.form.markAsDirty();
    this.message.set('PI, PS and fallback RadioText filled. Save to apply.');
  }
  draft(): ModulatorDraft {
    const v = this.form.getRawValue();
    if (!frequencyOptions.some((o) => o.value === v.frequency_khz))
      throw new Error('Select a frequency.');
    if (!rfOutputOptions.some((o) => o.value === v.rf_attenuation_db))
      throw new Error('Select RF output.');
    if (v.audio_mode === 'DIGITAL' && v.input_source_id === null)
      throw new Error('Select a digital source, or choose Analog input.');
    const af = v.af.map((value) => khz(value)!);
    if (af.length > 25 || af.some((f) => f < 87600 || f > 107900 || f % 100 !== 0))
      throw new Error('AF: use up to 25 frequencies from 87.6 to 107.9 MHz in 0.1 MHz steps.');
    if (new Set(af).size !== af.length) throw new Error('Remove duplicate AF frequencies.');
    const ps = v.rds_ps.map((p) => {
      if (
        !p.duration_s.trim() ||
        !Number.isFinite(Number(p.duration_s)) ||
        Number(p.duration_s) <= 0
      )
        throw new Error('PS duration must be greater than zero seconds.');
      return { text: checkText(p.text, 8, true), duration_s: Number(p.duration_s) };
    });
    if (!ps.length) throw new Error('Add at least one PS entry.');
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
      rds_pi: parseHex(v.pi, 4)!,
      rds_ecc: parseHex(v.ecc, 2, true),
      rds_pty: v.rds_pty ?? this.storedPty,
      rds_tp: v.rds_tp,
      rds_ps: ps,
      rds_af_khz: af,
      rds_rt: checkText(v.rds_rt, 64),
      rds_metadata_to_rt: v.rds_metadata_to_rt,
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
      const saved = await firstValueFrom(this.api.save(this.id, body));
      this.load(saved);
      this.message.set('Configuration saved.');
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
      !this.saving() && (!this.form.dirty || window.confirm('Discard unsaved Modulator changes?'))
    );
  }
  @HostListener('window:beforeunload', ['$event']) beforeUnload(event: BeforeUnloadEvent) {
    if (this.form.dirty || this.saving()) {
      event.preventDefault();
      event.returnValue = '';
    }
  }
}
export const modulatorDirtyGuard: CanDeactivateFn<ModulatorEditor> = (c) => c.canLeave();
