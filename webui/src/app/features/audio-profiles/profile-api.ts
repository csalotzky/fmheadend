import { BasicConfig } from './basic-config';
import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { timeout } from 'rxjs';
export type Processor = 'NONE' | 'GST_BASIC';
export interface ProfileDraft {
  schema_version: 1;
  name: string;
  stages: {
    gain: { selected: 'GST_VOLUME'; implementations: { GST_VOLUME: { gain_db: number } } };
    processor: { selected: Processor; implementations: { GST_BASIC: BasicConfig } };
  };
}
export interface AudioProfile {
  name: string;
  path: string;
  kind: 'default' | 'custom';
}
export interface ProfileResult extends AudioProfile {
  profile: ProfileDraft;
}
export interface ProfileSaveResult extends ProfileResult {
  saved?: true;
  applied?: boolean;
  restart_errors?: Record<string, string>;
}
const base = '/api/v1/audio-profiles/files';
export const profileUrl = (path: string) =>
  base + '/' + path.split('/').map(encodeURIComponent).join('/');
@Injectable({ providedIn: 'root' })
export class ProfileApi {
  private http = inject(HttpClient);
  list() {
    return this.http.get<AudioProfile[]>(base).pipe(timeout(15000));
  }
  get(path: string) {
    return this.http.get<ProfileResult>(profileUrl(path)).pipe(timeout(15000));
  }
  save(body: ProfileDraft, path?: string) {
    return (
      path === undefined
        ? this.http.post<ProfileSaveResult>(base + '/custom', body)
        : this.http.put<ProfileSaveResult>(profileUrl(path), body)
    ).pipe(timeout(300000));
  }
  clone(path: string, name: string) {
    return this.http
      .post<ProfileResult>(profileUrl(path) + '/clone', { name })
      .pipe(timeout(15000));
  }
  delete(path: string) {
    return this.http.delete<void>(profileUrl(path)).pipe(timeout(15000));
  }
}
