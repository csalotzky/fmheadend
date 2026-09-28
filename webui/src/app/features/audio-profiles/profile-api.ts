import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { timeout } from 'rxjs';
export interface DspStage {
  selected: string;
  implementations: Record<string, Record<string, any>>;
}
export interface DspSchema {
  slider?: {
    min: number;
    max: number;
    scale: 'linear' | 'log';
    offset?: number;
    step?: number;
    significant_digits?: number;
  };
  type?: string;
  title?: string;
  description?: string;
  hidden?: boolean;
  default?: any;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  multipleOf?: number;
  enum?: any[];
  enum_labels?: Record<string, string>;
  const?: any;
  properties?: Record<string, DspSchema>;
  items?: DspSchema;
  $ref?: string;
  $defs?: Record<string, DspSchema>;
  minItems?: number;
  maxItems?: number;
  active_count_from?: string;
  enabled_when?: string;
  disabled_when?: Record<string, any>;
}
export interface DspDescription {
  helpers?: Record<string, string>;
  template: ProfileDraft;
  stages: { key: string; label: string; choices: string[] }[];
  implementations: Record<
    string,
    {
      label: string;
      available: boolean;
      reason: string | null;
      schema: DspSchema;
      defaults: Record<string, any>;
      documentation_url?: string;
    }
  >;
}
export interface ProfileDraft {
  schema_version: 1;
  name: string;
  stages: Record<string, DspStage>;
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
  dsp() {
    return this.http.get<DspDescription>(base + '/dsp').pipe(timeout(15000));
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
