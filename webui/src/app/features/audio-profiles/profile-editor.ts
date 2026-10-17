import { ChangeDetectorRef, Component, HostListener, OnInit, inject, signal } from '@angular/core';
import {
  FormControl,
  FormGroup,
  ReactiveFormsModule,
  Validators,
  FormsModule,
} from '@angular/forms';
import { ActivatedRoute, CanDeactivateFn, Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { ProfileApi, ProfileDraft, DspDescription } from './profile-api';
import { DspFields } from './dsp-fields';
import { validSettings } from './dsp-number';
import { errorMessage } from '../../core/api/headend-api';
@Component({
  host: { class: 'page-content editor-page' },
  selector: 'app-profile-editor',
  imports: [ReactiveFormsModule, FormsModule, DspFields],
  styleUrl: '../../shared/editor-layout.css',
  styles: `
    summary {
      cursor: pointer;
      font-weight: 600;
      margin-bottom: 12px;
    }
    .plugin-doc {
      display: inline-block;
      font-size: 0.8rem;
      margin: 0 0 0.6rem;
      color: var(--muted);
    }
    .plugin {
      margin: 0.7rem 0;
      display: block;
    }
  `,
  template: `
    <div class="page-header">
      <h1>{{ path === undefined ? 'Add Audio Profile' : 'Edit Audio Profile' }}</h1>
    </div>
    @if (failed()) {
      <p role="alert" class="error">{{ error() }}</p>
    }
    @if (loading()) {
      <p role="status">Loading audio profile...</p>
    }
    @if (!loading() && !failed() && document && description) {
      <form [formGroup]="form" (ngSubmit)="save()">
        <fieldset [disabled]="saving() || readOnly()">
          <section class="editor-panel" aria-label="Profile details">
            <div class="fields">
              <label>Name<input formControlName="name" required maxlength="200" /></label>
              <label>Description<textarea formControlName="description" rows="2"></textarea></label>
            </div>
          </section>
          @for (stage of description.stages; track stage.key; let index = $index) {
            <details class="stage editor-panel" open>
              <summary>{{ index + 1 }}. {{ stage.label }}</summary>
              @if (description.helpers?.[stage.key]) {
                <p class="muted">{{ description.helpers[stage.key] }}</p>
              }
              @if (stage.choices.length > 1) {
                <label class="plugin"
                  >Implementation
                  <select
                    [ngModel]="document.stages[stage.key].selected"
                    [ngModelOptions]="{ standalone: true }"
                    (ngModelChange)="select(stage.key, $event)"
                  >
                    @for (choice of stage.choices; track choice) {
                      <option
                        [value]="choice"
                        [title]="
                          choice === 'OFF' ? '' : description.implementations[choice].reason || ''
                        "
                        [disabled]="
                          choice !== 'OFF' && !description.implementations[choice].available
                        "
                      >
                        {{ choice === 'OFF' ? 'Off' : description.implementations[choice].label }}
                        {{
                          choice !== 'OFF' && !description.implementations[choice].available
                            ? '(unavailable)'
                            : ''
                        }}
                      </option>
                    }
                  </select>
                </label>
              }
              @for (choice of stage.choices; track choice) {
                @if (choice !== 'OFF' && !description.implementations[choice].available) {
                  <small class="muted"
                    >{{ description.implementations[choice].label }}:
                    {{ description.implementations[choice].reason }}</small
                  >
                }
              }
              @if (document.stages[stage.key].selected !== 'OFF') {
                @let impl = description.implementations[document.stages[stage.key].selected];
                @if (impl.documentation_url) {
                  <a
                    class="plugin-doc"
                    [href]="impl.documentation_url"
                    target="_blank"
                    rel="noopener noreferrer"
                    >Plugin documentation ↗</a
                  >
                }
                @if (!impl.available) {
                  <p class="error">{{ impl.reason }}</p>
                }
                <fieldset [disabled]="!impl.available">
                  <app-dsp-fields
                    [schema]="impl.schema"
                    [values]="
                      document.stages[stage.key].implementations[
                        document.stages[stage.key].selected
                      ]
                    "
                    (changed)="changed()"
                  />
                </fieldset>
              }
              @if (stage.key === 'limiter') {
                <small>PCM limiting does not guarantee FM deviation.</small>
              }
              @if (stage.key === 'lpf') {
                <small>Filtering can regrow peaks. Validate the final filtered PCM level.</small>
              }
              @if (stage.key === 'output_trim') {
                <small
                  >FM pre-emphasis is handled by the TX and is configured in Modulator
                  settings.</small
                >
              }
            </details>
          }
        </fieldset>
        <div class="editor-feedback" aria-live="polite">
          @if (message()) {
            <p role="status">{{ message() }}</p>
          }
          @if (error()) {
            <p role="alert" class="error">{{ error() }}</p>
          }
        </div>
        <div class="editor-actions">
          <button type="submit" [disabled]="readOnly() || saving() || form.invalid">
            {{ saving() ? 'Saving...' : 'Save' }}
          </button>
          <button type="button" (click)="cancel()" [disabled]="saving()">Cancel</button>
        </div>
      </form>
    }
  `,
})
export class ProfileEditor implements OnInit {
  private cdr = inject(ChangeDetectorRef);
  private api = inject(ProfileApi);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  path = this.route.snapshot.queryParamMap.get('path') ?? undefined;
  readonly readOnly = signal(false);
  readonly loading = signal(false);
  readonly failed = signal(false);
  readonly saving = signal(false);
  readonly error = signal('');
  readonly message = signal('');
  document?: ProfileDraft;
  description?: DspDescription;
  readonly form = new FormGroup({
    description: new FormControl('', { nonNullable: true }),
    name: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(200), Validators.pattern(/\S/)],
    }),
  });
  changed() {
    this.form.markAsDirty();
    this.cdr.markForCheck();
  }
  select(stage: string, implementation: string) {
    if (this.readOnly()) return;
    const entry = this.document!.stages[stage];
    entry.selected = implementation;
    if (implementation !== 'OFF' && !entry.implementations[implementation])
      entry.implementations[implementation] = structuredClone(
        this.description!.implementations[implementation].defaults,
      );
    this.changed();
  }
  async ngOnInit() {
    if (window.history.state?.profileSaved) this.message.set('Audio Profile saved.');
    this.loading.set(true);
    try {
      this.description = await firstValueFrom(this.api.dsp());
      if (this.path === undefined) this.document = structuredClone(this.description.template);
      else {
        const result = await firstValueFrom(this.api.get(this.path));
        this.readOnly.set(result.kind === 'default');
        if (this.readOnly())
          this.message.set('Default profiles are read-only. Clone from the list.');
        this.document = structuredClone(result.profile);
      }
      this.form.controls.name.setValue(this.document.name);
      this.form.controls.description.setValue(this.document.description);
      if (this.readOnly()) this.form.disable();
      this.form.markAsPristine();
    } catch (e) {
      this.error.set(e instanceof Error && !('status' in e) ? e.message : errorMessage(e));
      this.failed.set(true);
    } finally {
      this.loading.set(false);
    }
  }
  async save() {
    if (this.readOnly() || this.saving() || this.loading() || this.failed() || this.form.invalid)
      return;
    if (!this.document) return;
    for (const entry of Object.values(this.document.stages)) {
      if (
        entry.selected !== 'OFF' &&
        !validSettings(
          this.description!.implementations[entry.selected].schema,
          entry.implementations[entry.selected],
        )
      ) {
        this.error.set('Check the DSP values: enter numbers within the supported limits.');
        return;
      }
    }
    const body = structuredClone(this.document);
    body.name = this.form.controls.name.value;
    body.description = this.form.controls.description.value;
    this.saving.set(true);
    this.error.set('');
    this.message.set('');
    try {
      const creating = this.path === undefined;
      const result = await firstValueFrom(this.api.save(body, this.path));
      this.path = result.path;
      this.document = result.profile;
      this.form.markAsPristine();
      this.saving.set(false);
      if (result.applied === false) {
        this.error.set(
          'Profile saved, but audio restart failed: ' +
            Object.values(result.restart_errors ?? {}).join('; '),
        );
      } else {
        this.message.set(
          'Audio Profile saved.' + (result.applied ? ' Active sources using it restarted.' : ''),
        );
        if (creating)
          await this.router.navigate(['/audio-profiles/edit'], {
            queryParams: { path: result.path },
            replaceUrl: true,
            state: { profileSaved: true },
          });
      }
    } catch (e) {
      this.error.set(e instanceof Error && !('status' in e) ? e.message : errorMessage(e));
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
