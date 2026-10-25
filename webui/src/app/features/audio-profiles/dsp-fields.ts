import { ChangeDetectorRef, Component, inject, input, output } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DspNumber } from './dsp-number';
import { DspSchema } from './profile-api';

/** Profile-only controls; edit the existing object so hidden keys survive. */
@Component({
  selector: 'app-dsp-fields',
  styleUrl: '../../shared/editor-layout.css',
  imports: [FormsModule, DspNumber],
  template: `
    <div class="fields">
      @for (field of fields(); track field.key) {
        @if (
          field.schema.type === 'number' ||
          (field.schema.type === 'integer' &&
            !field.schema.enum &&
            field.schema.const === undefined)
        ) {
          <app-dsp-number
            [schema]="field.schema"
            [label]="field.schema.title || field.key"
            [value]="values()[field.key]"
            [disabled]="disabled(field.schema)"
            (changed)="set(field.key, $event)"
          />
        } @else if (field.schema.type === 'boolean' && field.schema.const === undefined && !field.schema.enum) {
          <label class="checkbox-control">
            <input
              type="checkbox"
              [ngModel]="values()[field.key]"
              (ngModelChange)="set(field.key, $event)"
              [ngModelOptions]="{ standalone: true }"
            />
            <span>{{ field.schema.title || field.key }}</span>
            @if (field.schema.description) {
              <small>{{ field.schema.description }}</small>
            }
          </label>
        } @else if (field.schema.type !== 'array') {
          <label
            >{{ field.schema.title || field.key }}
            @if (field.schema.const !== undefined) {
              <span>{{ field.schema.const }}</span>
            } @else if (field.schema.enum) {
              <select
                [ngModel]="values()[field.key]"
                (ngModelChange)="set(field.key, $event)"
                [ngModelOptions]="{ standalone: true }"
              >
                @for (option of field.schema.enum; track option) {
                  <option [ngValue]="option">
                    {{ field.schema.enum_labels?.[option] ?? option }}
                  </option>
                }
              </select>
            } @else {
              <input
                type="number"
                required
                [disabled]="disabled(field.schema)"
                [step]="field.schema.multipleOf || (field.schema.type === 'integer' ? 1 : 'any')"
                [min]="field.schema.minimum ?? field.schema.exclusiveMinimum ?? null"
                [max]="field.schema.maximum ?? field.schema.exclusiveMaximum ?? null"
                [ngModel]="values()[field.key]"
                (ngModelChange)="set(field.key, $event)"
                [ngModelOptions]="{ standalone: true }"
              />
            }
            @if (field.schema.description) {
              <small>{{ field.schema.description }}</small>
            }
          </label>
        }
      }
    </div>
    @for (field of fields(); track field.key) {
      @if (field.schema.type === 'array') {
        @for (band of visibleItems(field.key, field.schema); track $index; let i = $index) {
          @if (field.schema.items?.type === 'number') {
            <app-dsp-number
              [schema]="field.schema.items!"
              [label]="field.schema.title + ' ' + (i + 1)"
              [value]="band"
              (changed)="setItem(field.key, i, $event)"
            />
            <button type="button" (click)="remove(field.key, i)">Remove crossover</button>
          } @else {
            <details open>
              <summary>Band {{ i + 1 }}</summary>
              <app-dsp-fields
                [schema]="itemSchema(field.schema)"
                [values]="band"
                (changed)="changed.emit()"
              />
              @if (!field.schema.active_count_from) {
                <button
                  type="button"
                  (click)="remove(field.key, i)"
                  [disabled]="values()[field.key].length <= (field.schema.minItems || 0)"
                >
                  Remove band
                </button>
              }
            </details>
          }
        }
        @if (!field.schema.active_count_from) {
          <button
            type="button"
            (click)="add(field.key, field.schema)"
            [disabled]="values()[field.key].length >= (field.schema.maxItems || 16)"
          >
            {{ field.schema.items?.type === 'number' ? 'Add crossover / band' : 'Add band' }}
          </button>
        }
      }
    }
  `,
  styles: `
    details {
      border: 1px solid #36404c;
      padding: 0.6rem;
      margin: 0.5rem 0;
    }
  `,
})
export class DspFields {
  private cdr = inject(ChangeDetectorRef);
  schema = input.required<DspSchema>();
  values = input.required<Record<string, any>>();
  changed = output<void>();
  private notifyChanged() {
    this.cdr.markForCheck();
    this.changed.emit();
  }
  fields() {
    return Object.entries(this.schema().properties ?? {})
      .filter(
        ([, s]) =>
          !s.hidden &&
          Object.entries(s.visible_when ?? {}).every(
            ([key, value]) => this.values()[key] === value,
          ),
      )
      .map(([key, schema]) => ({ key, schema }));
  }
  disabled(s: DspSchema) {
    return (
      !!(s.enabled_when && !this.values()[s.enabled_when]) ||
      Object.entries(s.disabled_when ?? {}).some(([k, v]) => this.values()[k] === v)
    );
  }
  set(key: string, value: any) {
    this.values()[key] = value;
    this.notifyChanged();
  }
  setItem(key: string, i: number, value: number) {
    this.values()[key][i] = value;
    this.notifyChanged();
  }
  visibleItems(key: string, s: DspSchema): any[] {
    return s.active_count_from
      ? this.values()[key].slice(0, this.values()[s.active_count_from].length + 1)
      : this.values()[key];
  }
  itemSchema(s: DspSchema): DspSchema {
    return s.items?.$ref ? this.schema().$defs![s.items.$ref.split('/').pop()!] : s.items!;
  }
  remove(key: string, i: number) {
    this.values()[key].splice(i, 1);
    this.notifyChanged();
  }
  add(key: string, s: DspSchema) {
    if (s.items?.type === 'number') {
      const values = this.values()[key];
      values.push(values.length ? (values[values.length - 1] + 20000) / 2 : 1000);
      this.notifyChanged();
      return;
    }
    const properties = this.itemSchema(s).properties ?? {};
    this.values()[key].push(
      Object.fromEntries(
        Object.entries(properties).map(([k, v]) => [k, structuredClone(v.default)]),
      ),
    );
    this.notifyChanged();
  }
}
