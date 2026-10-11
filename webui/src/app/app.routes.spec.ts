import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Title } from '@angular/platform-browser';
import { provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { routes } from './app.routes';

@Component({ template: '' })
class RouteContent {}

describe('Product document title', () => {
  it('keeps FMheadend through every page, editor, and redirect', async () => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter(
          routes.map((route) => ({
            ...route,
            component: route.component ? RouteContent : undefined,
            canDeactivate: [],
          })),
        ),
      ],
    });
    const harness = await RouterTestingHarness.create();
    for (const url of [
      '/',
      '/sources',
      '/sources/new',
      '/sources/1',
      '/modulators',
      '/modulators/0',
      '/audio-profiles',
      '/audio-profiles/new',
      '/audio-profiles/edit?path=default%2Fmoderate_loud.json',
      '/system',
      '/unknown',
      '/dashboard',
    ]) {
      await harness.navigateByUrl(url);
      expect(TestBed.inject(Title).getTitle()).toBe('FMheadend');
    }
  });
});
