import { Component, input, output } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DspSchema } from './profile-api';

let nextId = 0;

export function validNumber(s: DspSchema, v: number): boolean {
  return (
    Number.isFinite(v) &&
    (s.minimum === undefined || v >= s.minimum) &&
    (s.maximum === undefined || v <= s.maximum) &&
    (s.exclusiveMinimum === undefined || v > s.exclusiveMinimum) &&
    (s.exclusiveMaximum === undefined || v < s.exclusiveMaximum) &&
    (s.type !== 'integer' || Number.isInteger(v)) &&
    (s.multipleOf === undefined || Math.abs(v / s.multipleOf - Math.round(v / s.multipleOf)) < 1e-8)
  );
}

export function validSettings(
  schema: DspSchema,
  values: Record<string, any>,
  root = schema,
): boolean {
  return Object.entries(schema.properties ?? {}).every(([key, field]) => {
    const value = values[key];
    if (field.type === 'number' || field.type === 'integer') return validNumber(field, value);
    if (field.type === 'array') {
      const item = field.items?.$ref
        ? root.$defs![field.items.$ref.split('/').pop()!]
        : field.items!;
      return (
        Array.isArray(value) &&
        value.every((v) =>
          item.type === 'number' ? validNumber(item, v) : validSettings(item, v, root),
        )
      );
    }
    return true;
  });
}

/** Slider gestures quantize; precise entry and loading never do. Values remain in profile units. */
@Component({
  selector: 'app-dsp-number',
  styleUrl: '../../shared/editor-layout.css',
  imports: [FormsModule],
  template: `
    <label [for]="id">{{ label() }}</label>
    <div class="pair">
      @if (schema().slider) {
        <input
          type="range"
          min="0"
          max="1000"
          step="1"
          [value]="position()"
          [disabled]="disabled()"
          [attr.aria-label]="label() + ' slider'"
          [attr.aria-valuetext]="value() + ' ' + label()"
          (input)="slide($any($event.target).valueAsNumber)"
        />
      }
      <input
        [id]="id"
        type="number"
        required
        [disabled]="disabled()"
        [step]="schema().multipleOf || (schema().type === 'integer' ? 1 : 'any')"
        [min]="schema().minimum ?? schema().exclusiveMinimum ?? null"
        [max]="schema().maximum ?? schema().exclusiveMaximum ?? null"
        [ngModel]="value()"
        (ngModelChange)="changed.emit($event)"
        [ngModelOptions]="{ standalone: true }"
      />
    </div>
    @if (!disabled() && invalid()) {
      <small role="alert">Enter a value within the supported limits.</small>
    }
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    label {
      display: block;
      margin-bottom: 0.3rem;
    }
    .pair {
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    input[type='range'] {
      flex: 1;
      min-width: 0;
      width: 100%;
      padding: 0;
      accent-color: var(--warning);
    }
    input[type='number'] {
      width: 7rem;
      min-width: 0;
    }
    small {
      color: var(--error);
    }
  `,
})
export class DspNumber {
  readonly id = `dsp-number-${nextId++}`;
  schema = input.required<DspSchema>();
  label = input.required<string>();
  value = input.required<number>();
  disabled = input(false);
  changed = output<number>();
  invalid() {
    return !validNumber(this.schema(), this.value());
  }
  bounds() {
    const s = this.schema().slider!,
      v = this.value();
    // Finite slider window can expand for valid saved precision/very low gain.
    return this.invalid() ? [s.min, s.max] : [Math.min(s.min, v), Math.max(s.max, v)];
  }
  position() {
    const s = this.schema().slider!,
      [low, high] = this.bounds(),
      offset = s.offset ?? 0;
    if (this.invalid()) return 0;
    const f = (v: number) => (s.scale === 'log' ? Math.log(v + offset) : v);
    return (1000 * (f(this.value()) - f(low))) / (f(high) - f(low));
  }
  slide(position: number) {
    const s = this.schema().slider!,
      [low, high] = this.bounds(),
      offset = s.offset ?? 0;
    if (position === 0 || position === 1000) {
      this.changed.emit(position === 0 ? low : high);
      return;
    }
    const raw =
      s.scale === 'log'
        ? Math.exp(
            Math.log(low + offset) + (position / 1000) * Math.log((high + offset) / (low + offset)),
          ) - offset
        : low + (position / 1000) * (high - low);
    const rounded = s.significant_digits
      ? Number(raw.toPrecision(s.significant_digits))
      : Number((Math.round(raw / s.step!) * s.step!).toFixed(8));
    this.changed.emit(Math.max(low, Math.min(high, rounded)));
  }
}
