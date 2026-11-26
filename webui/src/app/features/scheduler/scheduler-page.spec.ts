import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { sample } from '../dashboard/test-fixtures';
import { ScheduleRule } from './scheduler-api';
import { SchedulerPage } from './scheduler-page';

const endpoint = '/api/v1/schedules';
const rule: ScheduleRule = {
  id: 1,
  recurrence: 'daily',
  weekdays: [],
  time: '09:30',
  tx_id: 2,
  action: 'start',
  source_id: 1,
  enabled: true,
};

describe('Scheduler management', () => {
  let http: HttpTestingController;
  let fixture: ComponentFixture<SchedulerPage>;
  let root: HTMLElement;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(SchedulerPage);
    root = fixture.nativeElement;
    fixture.detectChanges();
  });
  afterEach(() => http.verify());

  async function load(rules: ScheduleRule[]) {
    http.expectOne({ url: endpoint, method: 'GET' }).flush(rules);
    http.expectOne('/api/v1/sources').flush(sample().sources);
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function click(label: string) {
    const button = [...root.querySelectorAll('button')].find(
      (b) => b.textContent.trim() === label,
    )!;
    expect(button.disabled).toBe(false);
    button.click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function select(name: string, label: string) {
    const control = root.querySelector<HTMLSelectElement>(`select[name="${name}"]`)!;
    control.selectedIndex = [...control.options].findIndex(
      (option) => option.textContent.trim() === label,
    );
    expect(control.selectedIndex).toBeGreaterThanOrEqual(0);
    control.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  it('defaults TX ON to the current Source and clears a selected Source for TX OFF', async () => {
    await load([]);
    await click('Add rule');
    const source = root.querySelector<HTMLSelectElement>('[name="source"]')!;
    expect(source.selectedOptions[0].textContent.trim()).toBe('Keep current source');
    await select('source', 'Studio feed');
    await select('action', 'TX OFF');
    expect(root.querySelector('[name="source"]')).toBeNull();
    await click('Save');
    const request = http.expectOne({ url: endpoint, method: 'POST' });
    expect(request.request.body).toMatchObject({ action: 'stop', source_id: null });
    const saved = { ...rule, ...request.request.body };
    request.flush(saved);
    await Promise.resolve();
    await load([saved]);
    expect(root.textContent).toContain('TX OFF');
  });

  it('requires an explicit Source and at least one selected weekday for a matching rule', async () => {
    await load([]);
    await click('Add rule');
    await select('action', 'Source Switch');
    expect(root.querySelector('[name="source"]')!.textContent).not.toContain('Keep current source');
    expect(root.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
    await select('source', 'Studio feed');
    await select('recurrence', 'Selected weekdays');
    expect(root.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
    root.querySelector<HTMLInputElement>('fieldset.fields input[type="checkbox"]')!.click();
    fixture.detectChanges();
    await click('Save');
    const request = http.expectOne({ url: endpoint, method: 'POST' });
    expect(request.request.body).toMatchObject({
      action: 'switch_source',
      source_id: 1,
      recurrence: 'selected_weekdays',
      weekdays: [0],
    });
    request.flush({ message: 'Rule could not be saved.' }, { status: 409, statusText: 'Conflict' });
    await fixture.whenStable();
    fixture.detectChanges();
    expect(root.querySelector('[role="alert"]')!.textContent).toContain('Rule could not be saved.');
    expect(root.querySelector('form')).not.toBeNull();
  });

  it('lists Source names and refreshes after editing, disabling and confirmed deletion', async () => {
    await load([rule]);
    expect(root.textContent).toContain('09:30');
    expect(root.textContent).toContain('TX 2 · TX ON');
    expect(root.textContent).toContain('Studio feed');
    await click('Edit');
    await select('source', 'Keep current source');
    await click('Save');
    const edit = http.expectOne({ url: `${endpoint}/1`, method: 'PATCH' });
    expect(edit.request.body).toMatchObject({ action: 'start', source_id: null });
    const edited = { ...rule, source_id: null };
    edit.flush(edited);
    await Promise.resolve();
    await load([edited]);
    await click('Disable');
    const disable = http.expectOne({ url: `${endpoint}/1`, method: 'PATCH' });
    expect(disable.request.body).toEqual({ enabled: false });
    const disabled = { ...edited, enabled: false };
    disable.flush(disabled);
    await Promise.resolve();
    await load([disabled]);
    expect(root.textContent).toContain('Disabled');
    await click('Delete');
    http.expectNone({ url: `${endpoint}/1`, method: 'DELETE' });
    await click('Confirm delete');
    http.expectOne({ url: `${endpoint}/1`, method: 'DELETE' }).flush(null);
    await Promise.resolve();
    await load([]);
    expect(root.textContent).toContain('No schedule rules configured.');
  });
});
