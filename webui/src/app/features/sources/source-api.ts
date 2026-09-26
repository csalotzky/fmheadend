import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { timeout } from 'rxjs';
import { InputSource } from '../../core/api/models';
export type SourceType = InputSource['type'];
export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
export interface SourceDraft {
  name: string;
  type: SourceType;
  config: Record<string, Json>;
  processor_mode: 'bypass' | 'builtin' | 'stereotool';
  input_gain_db: number;
  audio_profile_path: string | null;
  gst_options: Record<string, Record<string, Json>>;
}
export interface SourceRuntime {
  observation_age_s?: number;
  observation_stale?: boolean;
  source_id: number;
  state: 'IDLE' | 'CONNECTING' | 'BUFFERING' | 'RUNNING' | 'ERROR';
  last_error: string | null;
}
export interface SourceSaveResult extends InputSource {
  saved?: true;
  applied?: boolean;
  apply_error?: string | null;
}
export interface SourceTest {
  input_bitrate_bps?: number | null;
  station_name?: string | null;
  ok: boolean;
  mocked: boolean;
  codec: string | null;
  sample_rate: number | null;
  channels: number | null;
  output_caps: string | null;
  stream_title: string | null;
  message: string;
}
export interface Profile {
  path: string;
  kind: 'default' | 'custom';
  name: string;
}
@Injectable({ providedIn: 'root' })
export class SourceApi {
  private http = inject(HttpClient);
  get(id: number) {
    return this.http.get<InputSource>(`/api/v1/sources/${id}`).pipe(timeout(15000));
  }
  profiles() {
    return this.http.get<Profile[]>('/api/v1/audio-profiles/files').pipe(timeout(15000));
  }
  save(body: SourceDraft, id?: number) {
    return (
      id === undefined
        ? this.http.post<SourceSaveResult>('/api/v1/sources', body)
        : this.http.patch<SourceSaveResult>(`/api/v1/sources/${id}`, body)
    ).pipe(timeout(300000));
  }
  delete(id: number) {
    return this.http.delete<void>(`/api/v1/sources/${id}`).pipe(timeout(15000));
  }
  status(id: number) {
    return this.http.get<SourceRuntime>(`/api/v1/sources/${id}/status`).pipe(timeout(15000));
  }
  test(value: number | SourceDraft) {
    return (
      typeof value === 'number'
        ? this.http.post<SourceTest>(`/api/v1/sources/${value}/test`, {})
        : this.http.post<SourceTest>('/api/v1/sources/test', value)
    ).pipe(timeout(60000));
  }
}
