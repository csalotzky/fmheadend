import { NgTemplateOutlet } from '@angular/common';
import {
  computed,
  Component,
  ElementRef,
  OnDestroy,
  ViewChild,
  inject,
  output,
  signal,
} from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { Subscription } from 'rxjs';
import { errorMessage } from '../../core/api/headend-api';
import { RADIO_PAGE_SIZE, RadioBrowserApi, RadioOption, RadioStation } from './radio-browser-api';

@Component({
  selector: 'app-radio-browser-picker',
  imports: [ReactiveFormsModule, NgTemplateOutlet],
  templateUrl: './radio-browser-picker.html',
  styleUrl: './radio-browser-picker.css',
})
export class RadioBrowserPicker implements OnDestroy {
  @ViewChild('dialog', { static: true }) dialog!: ElementRef<HTMLDialogElement>;
  @ViewChild('search', { static: true }) searchInput!: ElementRef<HTMLInputElement>;
  @ViewChild('results', { static: true }) results!: ElementRef<HTMLElement>;
  private api = inject(RadioBrowserApi);
  readonly chosen = output<RadioStation>();
  readonly opened = signal(false);
  readonly loading = signal(false);
  readonly error = signal('');
  readonly countriesError = signal('');
  readonly tagsError = signal('');
  readonly countries = signal<RadioOption[]>([]);
  readonly tags = signal<RadioOption[]>([]);
  readonly stations = signal<RadioStation[]>([]);
  readonly selected = signal<RadioStation | null>(null);
  readonly page = signal(1);
  private readonly visitedThrough = signal(0);
  private readonly lastPage = signal<number | null>(null);
  readonly reachableThrough = computed(
    () => this.lastPage() ?? Math.min(5001, Math.max(1, this.visitedThrough() + 1)),
  );
  readonly pages = computed(() => Array.from({ length: this.reachableThrough() }, (_, i) => i + 1));
  readonly filters = new FormGroup({
    name: new FormControl('', { nonNullable: true }),
    countrycode: new FormControl('', { nonNullable: true }),
    tag: new FormControl('', { nonNullable: true }),
    hidebroken: new FormControl(true, { nonNullable: true }),
  });
  private names =
    typeof Intl.DisplayNames === 'function'
      ? new Intl.DisplayNames(['en'], { type: 'region' })
      : null;
  private requests = new Subscription();
  private searchRequest?: Subscription;
  private tagRequest?: Subscription;
  private debounce?: ReturnType<typeof setTimeout>;
  private generation = 0;
  private requestedPage = 1;
  private resetHistoryOnSuccess = false;
  private previousFocus: HTMLElement | null = null;
  private previousOverflow = '';
  private changes = this.filters.valueChanges.subscribe(() => this.filtersChanged());

  countryName(code: string) {
    try {
      return this.names?.of(code.toUpperCase()) || code;
    } catch {
      return code;
    }
  }
  open() {
    if (this.opened()) return;
    this.previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    this.previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    this.opened.set(true);
    this.selected.set(null);
    this.requests = new Subscription();
    const dialog = this.dialog.nativeElement;
    if (dialog.showModal) dialog.showModal();
    else dialog.setAttribute('open', '');
    this.searchInput.nativeElement.focus();
    this.loadCountries();
    this.filtersChanged(0);
  }
  close() {
    if (!this.opened()) return;
    this.opened.set(false);
    ++this.generation;
    clearTimeout(this.debounce);
    this.searchRequest?.unsubscribe();
    this.tagRequest?.unsubscribe();
    this.requests.unsubscribe();
    const dialog = this.dialog.nativeElement;
    if (dialog.close) dialog.close();
    else dialog.removeAttribute('open');
    document.body.style.overflow = this.previousOverflow;
    this.previousFocus?.focus();
  }
  cancel(event: Event) {
    event.preventDefault();
    this.close();
  }
  ngOnDestroy() {
    this.close();
    this.changes.unsubscribe();
  }
  loadCountries() {
    this.countriesError.set('');
    this.requests.add(
      this.api.countries().subscribe({
        next: (rows) =>
          this.countries.set(
            [...rows].sort((a, b) =>
              this.countryName(a.name).localeCompare(this.countryName(b.name)),
            ),
          ),
        error: (e) => this.countriesError.set(errorMessage(e)),
      }),
    );
  }
  filtersChanged(delay = 300) {
    if (!this.opened()) return;
    ++this.generation;
    clearTimeout(this.debounce);
    this.searchRequest?.unsubscribe();
    this.tagRequest?.unsubscribe();
    this.selected.set(null);
    this.stations.set([]);
    this.tags.set([]);
    this.page.set(1);
    this.visitedThrough.set(0);
    this.lastPage.set(null);
    this.loading.set(true);
    this.error.set('');
    this.tagsError.set('');
    this.requestedPage = 1;
    this.resetHistoryOnSuccess = false;
    this.debounce = setTimeout(() => {
      this.searchPage(1);
      this.loadTags();
    }, delay);
  }
  loadTags() {
    this.tagRequest?.unsubscribe();
    this.tagsError.set('');
    const name = this.filters.controls.tag.value.trim();
    if (!name) {
      this.tags.set([]);
      return;
    }
    const generation = this.generation;
    this.tagRequest = this.api.tags(name).subscribe({
      next: (rows) => {
        if (generation === this.generation) this.tags.set(rows);
      },
      error: (e) => {
        if (generation === this.generation) this.tagsError.set(errorMessage(e));
      },
    });
  }
  private searchPage(target: number) {
    this.searchRequest?.unsubscribe();
    this.requestedPage = target;
    this.selected.set(null);
    this.results.nativeElement.scrollTop = 0;
    this.loading.set(true);
    this.error.set('');
    const generation = this.generation;
    const filters = this.filters.getRawValue();
    this.searchRequest = this.api
      .search(
        { ...filters, name: filters.name.trim(), tag: filters.tag.trim() },
        (target - 1) * RADIO_PAGE_SIZE,
      )
      .subscribe({
        next: (page) => {
          if (generation !== this.generation) return;
          if (!page.stations.length && target > 1) {
            if (target === this.page() + 1 && this.stations().length) {
              // A full page does not prove the next one exists. Keep the last
              // nonempty page without an extra fetch or an empty-page flash.
              this.lastPage.set(this.page());
              this.visitedThrough.set(this.page());
              this.loading.set(false);
              return;
            }
            // A previously visited page disappeared from the live catalogue.
            // Rediscover from page one rather than claim obsolete boundaries.
            this.resetHistoryOnSuccess = true;
            this.searchPage(1);
            return;
          }
          if (this.resetHistoryOnSuccess) {
            this.visitedThrough.set(0);
            this.lastPage.set(null);
            this.resetHistoryOnSuccess = false;
          }
          this.selected.set(null);
          this.stations.set(page.stations);
          this.page.set(target);
          this.visitedThrough.update((previous) => Math.max(previous, target));
          if (page.stations.length < RADIO_PAGE_SIZE) {
            this.lastPage.set(target);
            this.visitedThrough.set(target);
          }
          this.results.nativeElement.scrollTop = 0;
          this.loading.set(false);
        },
        error: (e) => {
          if (generation !== this.generation) return;
          this.loading.set(false);
          this.error.set(errorMessage(e));
        },
      });
  }
  goToPage(target: number) {
    if (this.loading() || target === this.page() || target < 1 || target > this.reachableThrough())
      return;
    this.searchPage(target);
  }
  retry() {
    if (!this.loading()) this.searchPage(this.requestedPage);
  }
  useStation() {
    const station = this.selected();
    if (this.loading() || !station?.playable_url) return;
    this.close();
    this.chosen.emit(station);
  }
}
