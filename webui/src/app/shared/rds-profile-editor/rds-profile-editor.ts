import { Component, Input, ChangeDetectionStrategy } from '@angular/core';
import { ReactiveFormsModule } from '@angular/forms';
import { RdsProfileForm } from './rds-profile-form';
import { ptyLabels, afOptions, mhz, textLength } from '../../features/modulators/values';
@Component({
  changeDetection: ChangeDetectionStrategy.Eager,
  selector: 'app-rds-profile-editor',
  imports: [ReactiveFormsModule],
  templateUrl: './rds-profile-editor.html',
  styleUrl: '../../features/modulators/modulators.css',
})
export class RdsProfileEditor {
  @Input({ required: true }) model!: RdsProfileForm;
  readonly labels = ptyLabels;
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
