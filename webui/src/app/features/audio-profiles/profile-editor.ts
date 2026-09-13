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
    @if (error()) {
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
            <p class="muted">
              Currently gain only. Additional Basic processing is not implemented.
            </p>
          }
          @if (id !== undefined) {
            <p class="muted">Stop all transmitters using this profile before saving changes.</p>
          }
        </fieldset>
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
  readonly form = new FormGroup({
    name: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(200), Validators.pattern(/\S/)],
    }),
    gain_db: new FormControl<number | null>(null, Validators.max(20)),
    processor: new FormControl<Processor | null>(null),
  });
  async ngOnInit() {
    if (this.id === undefined) return;
    this.loading.set(true);
    try {
      const p = await firstValueFrom(this.api.get(this.id));
      this.form.setValue({ name: p.name, gain_db: p.gain_db, processor: p.processor });
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
