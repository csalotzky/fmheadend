import { NgTemplateOutlet } from '@angular/common';
import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Observable, firstValueFrom, forkJoin } from 'rxjs';
import { errorMessage } from '../../core/api/headend-api';
import { InputSource, Modulator } from '../../core/api/models';
import { sortSources } from '../../core/api/source-order';
import { SourceApi } from '../sources/source-api';
import { ModulatorApi } from '../modulators/modulator-api';
import { mhz } from '../modulators/values';
import { ScheduleDraft, ScheduleRule, SchedulerApi } from './scheduler-api';

type RuleEdit = Omit<ScheduleDraft, 'enabled'>;

@Component({
  host: { class: 'page-content' },
  selector: 'app-scheduler-page',
  imports: [FormsModule, NgTemplateOutlet],
  templateUrl: './scheduler-page.html',
  styleUrls: [
    '../../shared/list-layout.css',
    '../../shared/editor-layout.css',
    './scheduler-page.css',
  ],
})
export class SchedulerPage implements OnInit {
  private api = inject(SchedulerApi);
  private sourceApi = inject(SourceApi);
  private modulatorApi = inject(ModulatorApi);

  readonly rules = signal<ScheduleRule[]>([]);
  readonly sources = signal<InputSource[]>([]);
  readonly targets = signal<Modulator[]>([]);
  readonly loading = signal(false);
  readonly busy = signal(false);
  readonly error = signal('');
  readonly draft = signal<RuleEdit | null>(null);
  readonly editing = signal<number | null>(null);
  readonly deleting = signal<number | null>(null);

  readonly days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  readonly dayPresets = [
    { label: 'Every day', days: [0, 1, 2, 3, 4, 5, 6] },
    { label: 'Weekdays', days: [0, 1, 2, 3, 4] },
    { label: 'Weekend', days: [5, 6] },
  ];
  readonly actions = { start: 'TX ON', stop: 'TX OFF', switch_source: 'Source Switch' };

  ngOnInit() {
    void this.load();
  }

  async load() {
    this.loading.set(true);
    this.error.set('');
    try {
      const { rules, sources, targets } = await firstValueFrom(
        forkJoin({
          rules: this.api.list(),
          sources: this.sourceApi.list(),
          targets: this.modulatorApi.list(),
        }),
      );
      this.rules.set(rules);
      this.sources.set(sortSources(sources));
      this.targets.set(targets);
    } catch (error) {
      this.error.set(errorMessage(error));
    } finally {
      this.loading.set(false);
    }
  }

  edit(rule?: ScheduleRule) {
    if (this.draft() || this.busy() || this.loading()) return;
    const target = this.targets()[0];
    if (!rule && !target) return;
    this.error.set('');
    this.deleting.set(null);
    this.editing.set(rule?.id ?? null);
    if (rule) {
      const { id, enabled, ...draft } = rule;
      this.draft.set(draft);
    } else {
      this.draft.set({
        days_of_week: [...this.dayPresets[0].days],
        time: '08:00',
        tx_id: target.id,
        action: 'start',
        source_id: null,
      });
    }
  }

  cancel() {
    this.draft.set(null);
    this.editing.set(null);
    this.error.set('');
  }

  change(changes: Partial<RuleEdit>) {
    this.draft.update((draft) => draft && { ...draft, ...changes });
  }

  changeDay(day: number, checked: boolean) {
    const days = new Set(this.draft()?.days_of_week);
    if (checked) days.add(day);
    else days.delete(day);
    this.change({ days_of_week: [...days].sort() });
  }

  scheduleLabel(rule: ScheduleRule) {
    const days = rule.days_of_week;
    if (days.length === 7) return 'Daily';
    if (days.length === 5 && days.every((day) => day < 5)) return 'Mon–Fri';
    if (days.length === 2 && days.every((day) => day >= 5)) return 'Sat–Sun';
    return this.days.filter((_, day) => days.includes(day)).join(', ');
  }

  targetLabel(txId: number) {
    const target = this.targets().find(({ id }) => id === txId);
    if (!target) return `TX ${txId} (Unavailable)`;
    if (target.frequency_khz == null) return `TX ${txId} (Not configured)`;
    const frequency = mhz(target.frequency_khz);
    return `TX ${txId} (${frequency.includes('.') ? frequency : frequency + '.0'} MHz)`;
  }

  hasTarget(txId: number) {
    return this.targets().some(({ id }) => id === txId);
  }

  sourceLabel(rule: ScheduleRule) {
    if (rule.source_id === null) return 'Keep current source';
    return (
      this.sources().find((source) => source.id === rule.source_id)?.name ?? 'Source unavailable'
    );
  }

  canSave(rule: RuleEdit) {
    return (
      /^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(rule.time) &&
      rule.days_of_week.length > 0 &&
      (rule.action !== 'switch_source' || rule.source_id !== null)
    );
  }

  async save() {
    const draft = this.draft();
    if (!draft || !this.canSave(draft) || this.busy()) return;
    const body = {
      ...draft,
      source_id: draft.action === 'stop' ? null : draft.source_id,
    };
    const id = this.editing();
    await this.mutate(
      id === null ? this.api.create({ ...body, enabled: true }) : this.api.update(id, body),
      () => this.cancel(),
    );
  }

  toggle(rule: ScheduleRule) {
    return this.mutate(this.api.update(rule.id, { enabled: !rule.enabled }));
  }

  remove(id: number) {
    if (this.deleting() !== id) return;
    return this.mutate(this.api.delete(id), () => this.deleting.set(null));
  }

  private async mutate(request: Observable<unknown>, onSaved?: () => void) {
    if (this.busy() || this.loading()) return;
    this.busy.set(true);
    this.error.set('');
    try {
      await firstValueFrom(request);
      onSaved?.();
      await this.load();
    } catch (error) {
      this.error.set(errorMessage(error));
    } finally {
      this.busy.set(false);
    }
  }
}
