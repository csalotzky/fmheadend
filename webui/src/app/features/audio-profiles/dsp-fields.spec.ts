import { TestBed } from '@angular/core/testing';
import { DspFields } from './dsp-fields';
import { DspSchema } from './profile-api';

describe('Modular DSP settings controls', () => {
  async function fields(schema: DspSchema, values: Record<string, any>) {
    const f = TestBed.createComponent(DspFields);
    f.componentRef.setInput('schema', schema);
    f.componentRef.setInput('values', values);
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    return f;
  }
  it('Short-time Grow Off preserves its speed but disables the control', async () => {
    const values = { short_grow_enabled: false, short_grow_ms: 40, hidden_period: 400 };
    const f = await fields(
      {
        properties: {
          short_grow_enabled: { type: 'boolean', title: 'Short-time Grow enabled' },
          short_grow_ms: {
            type: 'number',
            title: 'Short-time Grow (ms)',
            enabled_when: 'short_grow_enabled',
          },
          hidden_period: { type: 'number', hidden: true },
        },
      },
      values,
    );
    const speed = f.nativeElement.querySelector('input[type="number"]') as HTMLInputElement;
    expect(speed.disabled).toBe(true);
    expect(values.short_grow_ms).toBe(40);
    f.componentInstance.set('short_grow_enabled', true);
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    expect(speed.disabled).toBe(false);
    expect(values.hidden_period).toBe(400);
    expect(f.nativeElement.querySelectorAll('input')).toHaveLength(2);
  });
  it('Notch does not present gain as an operational control', async () => {
    const f = await fields(
      { properties: { gain_db: { type: 'number', disabled_when: { filter: 'notch' } } } },
      { filter: 'notch', gain_db: 6 },
    );
    expect(f.nativeElement.querySelector('input').disabled).toBe(true);
    f.componentInstance.set('filter', 'bell');
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    expect(f.nativeElement.querySelector('input').disabled).toBe(false);
  });
  it('multiband crossover changes preserve eight saved band settings and bypass leaves bands present', async () => {
    const values = {
      crossovers_hz: [120, 1000, 5000],
      bands: Array.from({ length: 8 }, (_, i) => ({ bypass: false, threshold_dbfs: -i - 12 })),
    };
    const schema: DspSchema = {
      properties: {
        crossovers_hz: { type: 'array', items: { type: 'number' }, maxItems: 7 },
        bands: {
          type: 'array',
          active_count_from: 'crossovers_hz',
          minItems: 8,
          maxItems: 8,
          items: { $ref: '#/$defs/Band' },
        },
      },
      $defs: {
        Band: {
          properties: {
            bypass: { type: 'boolean', title: 'Compressor Bypass' },
            threshold_dbfs: { type: 'number' },
          },
        },
      },
    };
    const f = await fields(schema, values);
    expect(f.nativeElement.querySelectorAll('details')).toHaveLength(4);
    values.bands[0].bypass = true;
    f.detectChanges();
    expect(f.nativeElement.querySelectorAll('details')).toHaveLength(4);
    for (let i = 0; i < 4; i++)
      f.componentInstance.add('crossovers_hz', schema.properties!['crossovers_hz']);
    f.detectChanges();
    expect(f.nativeElement.querySelectorAll('details')).toHaveLength(8);
    f.componentInstance.remove('crossovers_hz', 6);
    f.detectChanges();
    expect(values.bands).toHaveLength(8);
    expect(values.bands[7].threshold_dbfs).toBe(-19);
    expect(values.bands[0].bypass).toBe(true);
  });
});
