import { BasicConfig } from './basic-config';
import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { timeout } from 'rxjs';
export type Processor = 'NONE' | 'GST_BASIC';
export interface ProfileDraft {
  name: string;
  gain_db?: number;
  processor?: Processor;
  basic_config?: BasicConfig;
}
export interface AudioProfile {
  id: number;
  name: string;
  gain_db: number;
  processor: Processor;
  timestamp: string;
  basic_config: BasicConfig;
}
export const processorLabel = (value: Processor) =>
  value === 'NONE' ? 'None (gain only)' : 'GStreamer Basic';
@Injectable({ providedIn: 'root' })
export class ProfileApi {
  private http = inject(HttpClient);
  list() {
    return this.http.get<AudioProfile[]>('/api/v1/audio-profiles').pipe(timeout(15000));
  }
  get(id: number) {
    return this.http.get<AudioProfile>(`/api/v1/audio-profiles/${id}`).pipe(timeout(15000));
  }
  save(body: ProfileDraft, id?: number) {
    return (
      id === undefined
        ? this.http.post<AudioProfile>('/api/v1/audio-profiles', body)
        : this.http.patch<AudioProfile>(`/api/v1/audio-profiles/${id}`, body)
    ).pipe(timeout(15000));
  }
  delete(id: number) {
    return this.http.delete<void>(`/api/v1/audio-profiles/${id}`).pipe(timeout(15000));
  }
}
