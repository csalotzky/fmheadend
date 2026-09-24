import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import {
  BasicConfig,
  basicDefaults,
  highpassOptions,
  lowpassOptions,
  ratioOptions,
  thresholdOptions,
  eqRange,
} from './basic-config';
import { Component, HostListener, OnInit, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, CanDeactivateFn, Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { ProfileApi, ProfileDraft, Processor } from './profile-api';
import { errorMessage } from '../../core/api/headend-api';
@Component({
  host: { class: 'page-content editor-narrow' },
  selector: 'app-profile-editor',
  imports: [ReactiveFormsModule],
  styleUrl: '../sources/sources.css',
  template: `
    <h1>{{ id === undefined ? 'Add Audio Profile' : 'Edit Audio Profile' }}</h1>
    @if (failed() && error()) {
      <p role="alert" class="error">{{ error() }}</p>
    }
    @if (loading()) {
      <p role="status">Loading audio profile...</p>
    }
    @if (!loading() && !failed()) {
      <form [formGroup]="form" (ngSubmit)="save()">
        <fieldset [disabled]="saving()">
          <div class="fields">
            <label>Name<input formControlName="name" required maxlength="200" /></label>
            <label
              >Gain (dB)<input type="number" step="any" max="20" formControlName="gain_db" /><small
                >Applies with either processor. Maximum +20 dB. Leave blank to use the
                default.</small
              ></label
            >
            <label
              >Processor<select formControlName="processor">
                <option [ngValue]="null" disabled>Default: None (gain only)</option>
                <option value="NONE">None (gain only)</option>
                <option value="GST_BASIC">GStreamer Basic</option>
              </select></label
            >
          </div>
          @if (form.controls.processor.value === 'GST_BASIC') {
            <section formGroupName="basic_config" aria-labelledby="basic-heading">
              <h2 id="basic-heading">Basic processing</h2>
              <div class="fields">
                <label
                  >High-pass<select formControlName="highpass_enabled">
                    <option [ngValue]="true">On</option>
                    <option [ngValue]="false">Off</option>
                  </select></label
                >
                <label
                  >High-pass cutoff<select formControlName="highpass_hz">
                    @for (hz of highpass; track hz) {
                      <option [ngValue]="hz">{{ hz }} Hz</option>
                    }
                  </select></label
                >
              </div>
              <h3>Equalizer</h3>
              <div class="fields">
                @for (band of bands; track band.key) {
                  <label
                    >{{ band.label }} (dB)<input
                      type="number"
                      [formControlName]="band.key"
                      [min]="eq.min"
                      [max]="eq.max"
                      [step]="eq.step"
                      required
                  /></label>
                }
              </div>
              <div class="fields">
                <label
                  >Compressor<select formControlName="compressor_enabled">
                    <option [ngValue]="true">On</option>
                    <option [ngValue]="false">Off</option>
                  </select></label
                >
                <label
                  >Threshold<select formControlName="compressor_threshold_dbfs">
                    @for (db of thresholds; track db) {
                      <option [ngValue]="db">{{ db }} dBFS</option>
                    }
                  </select></label
                >
                <label
                  >Ratio<select formControlName="compressor_ratio">
                    @for (ratio of ratios; track ratio) {
                      <option [ngValue]="ratio">{{ ratio }}:1</option>
                    }
                  </select></label
                >
              </div>
              <div class="fields">
                <label
                  >Low-pass<select formControlName="lowpass_enabled">
                    <option [ngValue]="true">On</option>
                    <option [ngValue]="false">Off</option>
                  </select></label
                >
                <label
                  >Low-pass cutoff<select formControlName="lowpass_hz">
                    @for (hz of lowpass; track hz) {
                      <option [ngValue]="hz">{{ (hz / 1000).toFixed(1) }} kHz</option>
                    }
                  </select></label
                >
                <label
                  >Limiter<select formControlName="limiter_enabled">
                    <option [ngValue]="true">On</option>
                    <option [ngValue]="false">Off</option>
                  </select></label
                >
              </div>
            </section>
          }
          @if (id !== undefined) {
            <p class="muted">Stop all transmitters using this profile before saving changes.</p>
          }
        </fieldset>
        <div class="editor-feedback" aria-live="polite">
          @if (error()) {
            <p role="alert" class="error">{{ error() }}</p>
          }
        </div>
        <div class="editor-actions">
          <button type="submit" [disabled]="saving() || form.invalid">
            {{ saving() ? 'Saving...' : 'Save' }}</button
          ><button type="button" (click)="cancel()" [disabled]="saving()">Cancel</button>
        </div>
      </form>
    }
  `,
})
export class ProfileEditor implements OnInit {
  private api = inject(ProfileApi);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  readonly id = this.route.snapshot.paramMap.has('id')
    ? Number(this.route.snapshot.paramMap.get('id'))
    : undefined;
  readonly loading = signal(false);
  readonly failed = signal(false);
  readonly saving = signal(false);
  readonly error = signal('');
  readonly highpass = highpassOptions;
  readonly lowpass = lowpassOptions;
  readonly ratios = ratioOptions;
  readonly thresholds = thresholdOptions;
  readonly eq = eqRange;
  readonly bands = [
    { key: 'eq_low_db', label: 'Low 100 Hz' },
    { key: 'eq_mid_db', label: 'Mid 1.1 kHz' },
    { key: 'eq_high_db', label: 'High 11 kHz' },
  ];
  readonly basic = new FormGroup({
    highpass_enabled: new FormControl(basicDefaults.highpass_enabled, { nonNullable: true }),
    highpass_hz: new FormControl<BasicConfig['highpass_hz']>(basicDefaults.highpass_hz, {
      nonNullable: true,
    }),
    eq_low_db: new FormControl(basicDefaults.eq_low_db, { nonNullable: true }),
    eq_mid_db: new FormControl(basicDefaults.eq_mid_db, { nonNullable: true }),
    eq_high_db: new FormControl(basicDefaults.eq_high_db, { nonNullable: true }),
    compressor_enabled: new FormControl(basicDefaults.compressor_enabled, { nonNullable: true }),
    compressor_threshold_dbfs: new FormControl(basicDefaults.compressor_threshold_dbfs, {
      nonNullable: true,
    }),
    compressor_ratio: new FormControl<BasicConfig['compressor_ratio']>(
      basicDefaults.compressor_ratio,
      { nonNullable: true },
    ),
    lowpass_enabled: new FormControl(basicDefaults.lowpass_enabled, { nonNullable: true }),
    lowpass_hz: new FormControl<BasicConfig['lowpass_hz']>(basicDefaults.lowpass_hz, {
      nonNullable: true,
    }),
    limiter_enabled: new FormControl(basicDefaults.limiter_enabled, { nonNullable: true }),
  });
  readonly form = new FormGroup({
    basic_config: this.basic,
    name: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(200), Validators.pattern(/\S/)],
    }),
    gain_db: new FormControl<number | null>(null, Validators.max(20)),
    processor: new FormControl<Processor | null>(null),
  });
  constructor() {
    this.form.controls.processor.valueChanges
      .pipe(takeUntilDestroyed())
      .subscribe(() => this.syncBasic());
    for (const control of [
      this.basic.controls.highpass_enabled,
      this.basic.controls.compressor_enabled,
      this.basic.controls.lowpass_enabled,
    ])
      control.valueChanges.pipe(takeUntilDestroyed()).subscribe(() => this.syncBasic());
    this.syncBasic();
  }
  private syncBasic() {
    if (this.form.controls.processor.value !== 'GST_BASIC') {
      this.basic.disable({ emitEvent: false });
      return;
    }
    this.basic.enable({ emitEvent: false });
    const b = this.basic.controls;
    b.highpass_hz[b.highpass_enabled.value ? 'enable' : 'disable']({ emitEvent: false });
    b.compressor_threshold_dbfs[b.compressor_enabled.value ? 'enable' : 'disable']({
      emitEvent: false,
    });
    b.compressor_ratio[b.compressor_enabled.value ? 'enable' : 'disable']({ emitEvent: false });
    b.lowpass_hz[b.lowpass_enabled.value ? 'enable' : 'disable']({ emitEvent: false });
  }
  async ngOnInit() {
    if (this.id === undefined) return;
    this.loading.set(true);
    try {
      const p = await firstValueFrom(this.api.get(this.id));
      this.form.setValue({
        name: p.name,
        gain_db: p.gain_db,
        processor: p.processor,
        basic_config: p.basic_config,
      });
      this.syncBasic();
      this.form.markAsPristine();
    } catch (e) {
      this.error.set(errorMessage(e));
      this.failed.set(true);
    } finally {
      this.loading.set(false);
    }
  }
  async save() {
    if (this.saving() || this.loading() || this.failed() || this.form.invalid) return;
    const v = this.form.getRawValue();
    const body: ProfileDraft = { name: v.name };
    if (v.gain_db !== null) body.gain_db = v.gain_db;
    else if (this.id !== undefined) {
      this.error.set('Enter gain for this existing profile.');
      return;
    }
    if (v.processor !== null) body.processor = v.processor;
    if (this.id !== undefined || v.processor === 'GST_BASIC' || this.basic.dirty)
      body.basic_config = v.basic_config;
    this.saving.set(true);
    this.error.set('');
    try {
      await firstValueFrom(this.api.save(body, this.id));
      this.form.markAsPristine();
      this.saving.set(false);
      await this.router.navigate(['/audio-profiles']);
    } catch (e) {
      this.error.set(errorMessage(e));
    } finally {
      this.saving.set(false);
    }
  }
  cancel() {
    void this.router.navigate(['/audio-profiles']);
  }
  canLeave() {
    return (
      !this.saving() &&
      (!this.form.dirty || window.confirm('Discard unsaved Audio Profile changes?'))
    );
  }
  @HostListener('window:beforeunload', ['$event']) beforeUnload(event: BeforeUnloadEvent) {
    if (this.form.dirty || this.saving()) {
      event.preventDefault();
      event.returnValue = '';
    }
  }
}
export const profileDirtyGuard: CanDeactivateFn<ProfileEditor> = (c) => c.canLeave();
