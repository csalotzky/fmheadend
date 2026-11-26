import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Observable, firstValueFrom, forkJoin } from 'rxjs';
import { errorMessage } from '../../core/api/headend-api';
import { InputSource } from '../../core/api/models';
import { sortSources } from '../../core/api/source-order';
import { SourceApi } from '../sources/source-api';
import { ScheduleDraft, ScheduleRule, SchedulerApi } from './scheduler-api';

@Component({
  host: { class: 'page-content' },
  selector: 'app-scheduler-page',
  imports: [FormsModule],
  templateUrl: './scheduler-page.html',
  styleUrls: ['../../shared/list-layout.css', '../../shared/editor-layout.css'],
})
export class SchedulerPage implements OnInit {
  private api = inject(SchedulerApi);
  private sourceApi = inject(SourceApi);

  readonly rules = signal<ScheduleRule[]>([]);
  readonly sources = signal<InputSource[]>([]);
  readonly loading = signal(false);
  readonly busy = signal(false);
  readonly error = signal('');
  readonly draft = signal<ScheduleDraft | null>(null);
  readonly editing = signal<number | null>(null);
  readonly deleting = signal<number | null>(null);

  readonly weekdays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  readonly targets = [0, 1, 2, 3, 4, 5, 6, 7];
  readonly recurrences = {
    daily: 'Daily',
    weekdays: 'Weekdays',
    weekends: 'Weekends',
    selected_weekdays: 'Selected weekdays',
  };
  readonly actions = { start: 'TX ON', stop: 'TX OFF', switch_source: 'Source Switch' };

  ngOnInit() {
    void this.load();
  }

  async load() {
    this.loading.set(true);
    this.error.set('');
    try {
      const { rules, sources } = await firstValueFrom(
        forkJoin({ rules: this.api.list(), sources: this.sourceApi.list() }),
      );
      this.rules.set(rules);
      this.sources.set(sortSources(sources));
    } catch (error) {
      this.error.set(errorMessage(error));
    } finally {
      this.loading.set(false);
    }
  }

  edit(rule?: ScheduleRule) {
    this.error.set('');
    this.deleting.set(null);
    this.editing.set(rule?.id ?? null);
    if (rule) {
      const { id, ...draft } = rule;
      this.draft.set(draft);
    } else {
      this.draft.set({
        recurrence: 'daily',
        weekdays: [],
        time: '08:00',
        tx_id: 0,
        action: 'start',
        source_id: null,
        enabled: true,
      });
    }
  }

  change(changes: Partial<ScheduleDraft>) {
    this.draft.update((draft) => draft && { ...draft, ...changes });
  }

  changeWeekday(day: number, checked: boolean) {
    const days = this.draft()?.weekdays ?? [];
    this.change({ weekdays: checked ? [...days, day].sort() : days.filter((d) => d !== day) });
  }

  scheduleLabel(rule: ScheduleRule) {
    return rule.recurrence === 'selected_weekdays'
      ? this.weekdays.filter((_, day) => rule.weekdays.includes(day)).join(', ')
      : this.recurrences[rule.recurrence];
  }

  sourceLabel(rule: ScheduleRule) {
    if (rule.source_id === null) return 'Keep current source';
    return (
      this.sources().find((source) => source.id === rule.source_id)?.name ?? 'Source unavailable'
    );
  }

  canSave(rule: ScheduleDraft) {
    return (
      /^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(rule.time) &&
      (rule.recurrence !== 'selected_weekdays' || rule.weekdays.length > 0) &&
      (rule.action !== 'switch_source' || rule.source_id !== null)
    );
  }

  async save() {
    const draft = this.draft();
    if (!draft || !this.canSave(draft) || this.busy()) return;
    const body = {
      ...draft,
      weekdays: draft.recurrence === 'selected_weekdays' ? draft.weekdays : [],
      source_id: draft.action === 'stop' ? null : draft.source_id,
    };
    const id = this.editing();
    await this.mutate(id === null ? this.api.create(body) : this.api.update(id, body), () => {
      this.draft.set(null);
    });
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
