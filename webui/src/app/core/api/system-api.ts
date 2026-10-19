import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { timeout } from 'rxjs';
export interface FrequencyPlan {
  region: number;
  region_name: string;
  fm_min_khz: number | null;
  fm_max_khz: number | null;
  fm_step_khz: number | null;
}
export interface SystemInfo {
  fmheadend_version: string;
  controller_protocol_version: number | null;
  controller_firmware_version: string | null;
  hardware_target: string | null;
  modulator_count: number | null;
  mocked: boolean;
  frequency_plan: FrequencyPlan;
  frequency_min_khz: number | null;
  frequency_max_khz: number | null;
  frequency_step_khz: number | null;
  database_schema_version: number;
  media_backend: string;
}
export interface RecentLog {
  timestamp: string;
  severity: string;
  logger: string;
  message: string;
}
export function frequencyLabel(khz: number): string {
  const fraction = String(khz % 1000)
    .padStart(3, '0')
    .replace(/0+$/, '');
  return `${Math.floor(khz / 1000)}.${fraction || '0'} MHz`;
}
export function planOptions(plan: FrequencyPlan) {
  if (plan.fm_min_khz === null || plan.fm_max_khz === null || plan.fm_step_khz === null) return [];
  const { fm_min_khz: minimum, fm_max_khz: maximum, fm_step_khz: step } = plan;
  return Array.from(
    { length: Math.floor((maximum - minimum) / step) + 1 },
    (_, i) => {
      const value = minimum + i * step;
      return { value, label: frequencyLabel(value) };
    },
  );
}
@Injectable({ providedIn: 'root' })
export class SystemApi {
  private http = inject(HttpClient);
  logs() {
    return this.http.get<RecentLog[]>('/api/v1/system/logs').pipe(timeout(15000));
  }
  info() {
    return this.http.get<SystemInfo>('/api/v1/system/info').pipe(timeout(15000));
  }
}
