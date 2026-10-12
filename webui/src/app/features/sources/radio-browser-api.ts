import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { timeout } from 'rxjs';

export const RADIO_PAGE_SIZE = 20;

export interface RadioStation {
  stationuuid: string;
  name: string;
  url: string;
  url_resolved: string | null;
  playable_url: string | null;
  countrycode: string;
  country: string;
  tags: string[];
  codec: string;
  bitrate: number;
  favicon: string | null;
  lastcheckok: boolean | null;
  hls: boolean;
}
export interface RadioOption {
  name: string;
  stationcount: number;
}
export interface RadioSearch {
  name: string;
  countrycode: string;
  tag: string;
  hidebroken: boolean;
}
export interface RadioPage {
  stations: RadioStation[];
  offset: number;
  limit: number;
}
@Injectable({ providedIn: 'root' })
export class RadioBrowserApi {
  private http = inject(HttpClient);
  private readonly root = '/api/v1/radio-browser';
  search(filters: RadioSearch, offset = 0) {
    const { countrycode, tag, ...rest } = filters;
    return this.http
      .get<RadioPage>(this.root + '/stations/search', {
        params: {
          ...rest,
          ...(countrycode ? { countrycode } : {}),
          ...(tag ? { tag } : {}),
          offset,
          limit: RADIO_PAGE_SIZE,
        },
      })
      .pipe(timeout(20000));
  }
  lookup(uuid: string) {
    return this.http
      .get<RadioStation>(this.root + '/stations/' + encodeURIComponent(uuid))
      .pipe(timeout(20000));
  }
  countries() {
    return this.http
      .get<RadioOption[]>(this.root + '/countrycodes', { params: { limit: 250 } })
      .pipe(timeout(20000));
  }
  tags(name: string) {
    return this.http
      .get<RadioOption[]>(this.root + '/tags', { params: { name, limit: 20 } })
      .pipe(timeout(20000));
  }
}
