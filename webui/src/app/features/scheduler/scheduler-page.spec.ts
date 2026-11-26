import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { sample } from '../dashboard/test-fixtures';
import { ScheduleRule } from './scheduler-api';
import { SchedulerPage } from './scheduler-page';

const endpoint = '/api/v1/schedules';
const rule: ScheduleRule = {
  id: 1,
  days_of_week: [0, 1, 2, 3, 4, 5, 6],
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
    const modulators = sample().modulators;
    http
      .expectOne('/api/v1/modulators')
      .flush([{ ...modulators[0], frequency_khz: 95000 }, modulators[2], modulators[7]]);
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function click(label: string, container = root) {
    const button = [...container.querySelectorAll('button')].find(
      (b) => b.textContent.trim() === label,
    )!;
    expect(button.disabled).toBe(false);
    button.click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  function cards() {
    return [...root.querySelectorAll<HTMLElement>('.list-card')];
  }

  function dayInputs() {
    return [...root.querySelectorAll<HTMLInputElement>('[aria-label="Days"] input')];
  }

  function checkedDays() {
    return dayInputs().flatMap((input, day) => (input.checked ? [day] : []));
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
    expect(request.request.body).toEqual({
      days_of_week: [0, 1, 2, 3, 4, 5, 6],
      time: '08:00',
      tx_id: 0,
      action: 'stop',
      source_id: null,
      enabled: true,
    });
    const saved = { ...rule, ...request.request.body };
    request.flush(saved);
    await Promise.resolve();
    await load([saved]);
    expect(root.textContent).toContain('TX OFF');
  });

  it('requires an explicit Source and at least one selected day', async () => {
    await load([]);
    await click('Add rule');
    await select('action', 'Source Switch');
    expect(root.querySelector('[name="source"]')!.textContent).not.toContain('Keep current source');
    expect(root.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
    await select('source', 'Studio feed');
    for (const input of dayInputs()) input.click();
    fixture.detectChanges();
    expect(root.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
    dayInputs()[0].click();
    fixture.detectChanges();
    await click('Save');
    const request = http.expectOne({ url: endpoint, method: 'POST' });
    expect(request.request.body).toMatchObject({
      action: 'switch_source',
      source_id: 1,
      days_of_week: [0],
    });
    request.flush({ message: 'Rule could not be saved.' }, { status: 409, statusText: 'Conflict' });
    await fixture.whenStable();
    fixture.detectChanges();
    expect(root.querySelector('[role="alert"]')!.textContent).toContain('Rule could not be saved.');
    expect(root.querySelector('form')).not.toBeNull();
  });

  it('lists Source names and refreshes after editing, disabling and confirmed deletion', async () => {
    await load([{ ...rule, enabled: false }]);
    expect(root.textContent).toContain('09:30');
    expect(root.textContent).toContain('TX 2 (92.0 MHz) · TX ON');
    expect(root.textContent).toContain('Studio feed');
    await click('Edit');
    await select('source', 'Keep current source');
    await click('Save');
    const edit = http.expectOne({ url: `${endpoint}/1`, method: 'PATCH' });
    expect(edit.request.body).toEqual({
      action: 'start',
      source_id: null,
      days_of_week: rule.days_of_week,
      time: rule.time,
      tx_id: 2,
    });
    const edited = { ...rule, source_id: null, enabled: false };
    edit.flush(edited);
    await Promise.resolve();
    await load([edited]);
    expect(root.textContent).toContain('Disabled');
    await click('Enable');
    const enable = http.expectOne({ url: `${endpoint}/1`, method: 'PATCH' });
    expect(enable.request.body).toEqual({ enabled: true });
    const enabled = { ...edited, enabled: true };
    enable.flush(enabled);
    await Promise.resolve();
    await load([enabled]);
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

  it('edits the selected card in place and appends a new draft at the end', async () => {
    await load([rule, { ...rule, id: 2, time: '10:30' }, { ...rule, id: 3, time: '11:30' }]);
    const [first, second, third] = cards();
    await click('Edit', second);
    expect(cards()).toEqual([first, second, third]);
    expect(second.querySelector('form')).not.toBeNull();
    expect(root.querySelectorAll('form')).toHaveLength(1);
    expect(first.textContent).toContain('09:30');
    expect(third.textContent).toContain('11:30');
    expect(first.querySelector<HTMLButtonElement>('button')!.disabled).toBe(true);
    await select('action', 'TX OFF');
    await click('Cancel', second);
    expect(second.querySelector('form')).toBeNull();
    expect(second.textContent).toContain('TX ON');
    expect(second.textContent).toContain('Studio feed');
    await click('Add rule');
    expect(cards().slice(0, 3)).toEqual([first, second, third]);
    expect(cards()[3].querySelector('form')).not.toBeNull();
    await click('Cancel', cards()[3]);
    expect(cards()).toEqual([first, second, third]);
    http.expectNone((request) => request.method !== 'GET');
  });

  it('applies day presets locally and submits a numeric target with the selected days', async () => {
    await load([]);
    await click('Add rule');
    expect(checkedDays()).toEqual([0, 1, 2, 3, 4, 5, 6]);
    await click('Weekdays');
    expect(checkedDays()).toEqual([0, 1, 2, 3, 4]);
    await click('Weekend');
    expect(checkedDays()).toEqual([5, 6]);
    await click('Every day');
    expect(checkedDays()).toEqual([0, 1, 2, 3, 4, 5, 6]);
    await click('Weekend');
    dayInputs()[5].click();
    fixture.detectChanges();
    expect(checkedDays()).toEqual([6]);
    http.expectNone((request) => request.method !== 'GET');
    const tx = root.querySelector<HTMLSelectElement>('[name="tx"]')!;
    expect([...tx.options].map((option) => option.textContent.trim())).toEqual([
      'TX 0 (95.0 MHz)',
      'TX 2 (92.0 MHz)',
      'TX 7 (Not configured)',
    ]);
    await select('tx', 'TX 2 (92.0 MHz)');
    await click('Save');
    const request = http.expectOne({ url: endpoint, method: 'POST' });
    expect(request.request.body).toMatchObject({ days_of_week: [6], tx_id: 2 });
    const saved = { ...rule, ...request.request.body };
    request.flush(saved);
    await Promise.resolve();
    await load([saved]);
    expect(root.textContent).toContain('Sun');
  });

  it('shows friendly day labels and gives disabled strips precedence over action colors', async () => {
    await load([
      rule,
      { ...rule, id: 2, action: 'stop', source_id: null, days_of_week: [0, 1, 2, 3, 4] },
      { ...rule, id: 3, action: 'switch_source', days_of_week: [5, 6] },
      { ...rule, id: 4, enabled: false, days_of_week: [0, 2, 4] },
      { ...rule, id: 5, enabled: false, action: 'stop', source_id: null },
      { ...rule, id: 6, enabled: false, action: 'switch_source' },
    ]);
    expect(
      cards()
        .slice(0, 4)
        .map((card) => card.querySelector('.list-secondary')!.textContent),
    ).toEqual(['Daily', 'Mon–Fri', 'Sat–Sun', 'Mon, Wed, Fri']);
    expect(
      cards().map((card) => getComputedStyle(card).getPropertyValue('--list-strip-color').trim()),
    ).toEqual([
      'var(--success)',
      'var(--error)',
      'var(--blue)',
      ...Array(3).fill('var(--inactive)'),
    ]);
  });
});
