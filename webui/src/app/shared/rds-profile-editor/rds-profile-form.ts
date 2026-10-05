import { AbstractControl, FormArray, FormControl, FormGroup } from '@angular/forms';
import { RdsProfile } from '../../core/api/models';
import { hex, mhz, khz, parseHex, checkText } from '../../features/modulators/values';
const text = (value = '') => new FormControl(value, { nonNullable: true });
const flag = (value = false) => new FormControl(value, { nonNullable: true });
const psEntry = (value = '', duration = '5') =>
  new FormGroup({ text: text(value), duration_s: text(duration) });
export class RdsProfileForm {
  private storedPty = 0;
  form = new FormGroup({
    pi: text(),
    ecc: text(),
    rds_pty: new FormControl<number | null>(0),
    rds_tp: flag(),
    rds_ps: new FormArray([psEntry()]),
    af: new FormArray<FormControl<string>>([]),
    rds_rt: text(),
    rds_rt_enabled: flag(true),
    rds_rt_termination: new FormControl<'CR' | 'PAD'>('CR', { nonNullable: true }),
    rds_metadata_to_rt: flag(),
  });
  get ps() {
    return this.form.controls.rds_ps;
  }
  get af() {
    return this.form.controls.af;
  }
  attach<T extends typeof this.form.controls & { [K in keyof T]: AbstractControl }>(
    form: FormGroup<T>,
  ) {
    // The Modulator form adds hardware controls to these same RDS controls.
    // Angular's FormGroup is invariant; expose only the shared controls here.
    this.form = form as unknown as typeof this.form;
  }
  load(p: RdsProfile) {
    this.storedPty = p.rds_pty;
    this.form.patchValue({
      rds_ps: [],
      rds_pty: p.rds_pty <= 29 ? p.rds_pty : null,
      pi: hex(p.rds_pi, 4),
      ecc: hex(p.rds_ecc, 2),
      rds_tp: p.rds_tp,
      rds_rt: p.rds_rt,
      rds_rt_enabled: p.rds_rt_enabled,
      rds_rt_termination: p.rds_rt_termination,
      rds_metadata_to_rt: p.rds_metadata_to_rt,
    });
    this.ps.clear();
    p.rds_ps.forEach((e) => this.ps.push(psEntry(e.text, String(e.duration_s))));
    this.af.clear();
    p.rds_af_khz.forEach((f) => this.af.push(text(mhz(f))));
    this.form.markAsPristine();
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
  draft(): RdsProfile {
    const v = this.form.getRawValue();
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
      rds_pi: parseHex(v.pi, 4)!,
      rds_ecc: parseHex(v.ecc, 2, true),
      rds_pty: v.rds_pty ?? this.storedPty,
      rds_tp: v.rds_tp,
      rds_ps: ps,
      rds_af_khz: af,
      rds_rt: checkText(v.rds_rt, 64),
      rds_rt_enabled: v.rds_rt_enabled,
      rds_rt_termination: v.rds_rt_termination,
      rds_metadata_to_rt: v.rds_metadata_to_rt,
    };
  }
}
