import { Component, Input, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { SystemApi } from '../../core/api/system-api';
import { ReactiveFormsModule } from '@angular/forms';
import { RdsProfileForm } from './rds-profile-form';
import { ptyLabels, rbdsPtyLabels, afOptions, mhz, textLength } from '../../features/modulators/values';
@Component({
  changeDetection: ChangeDetectionStrategy.Eager,
  selector: 'app-rds-profile-editor',
  imports: [ReactiveFormsModule],
  templateUrl: './rds-profile-editor.html',
  styleUrl: './rds-profile-editor.css',
})
export class RdsProfileEditor {
  @Input({ required: true }) model!: RdsProfileForm;
  readonly labels = signal(ptyLabels);
  constructor() {
    inject(SystemApi).info().pipe(takeUntilDestroyed()).subscribe({
      next: (info) => this.labels.set(info.frequency_plan.region === 2 ? rbdsPtyLabels : ptyLabels),
      error: () => {}, // Keep the existing labels if system information is unavailable.
    });
  }
  readonly alternatives = afOptions;
  readonly mhz = mhz;
  readonly textLength = textLength;
  get form() {
    return this.model.form;
  }
  get ps() {
    return this.model.ps;
  }
  get af() {
    return this.model.af;
  }
  addPs() {
    this.model.addPs();
  }
  removePs(i: number) {
    this.model.removePs(i);
  }
  movePs(i: number, delta: number) {
    this.model.movePs(i, delta);
  }
  addAf() {
    this.model.addAf();
  }
  removeAf(i: number) {
    this.model.removeAf(i);
  }
}
