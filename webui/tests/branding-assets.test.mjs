import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = resolve(root, process.env.BRANDING_BUILD_DIR ?? 'dist/fmheadend-ui/browser');
const sizes = [16, 32, 48, 64, 128, 256];
const logo = 'fmheadend_logo_tower.svg';
const files = [logo, ...sizes.map((size) => `fmheadend_favicon_${size}.png`)];
const config = JSON.parse(await readFile(resolve(root, 'angular.json'), 'utf8'));
const build = config.projects['fmheadend-ui'].architect.build;

test('development and production use the root branding asset source', () => {
  assert.ok(
    build.options.assets.some(
      (asset) =>
        asset.input === 'tmp/branding-assets' && asset.output === 'assets' && asset.glob === '*',
    ),
  );
  for (const mode of ['development', 'production'])
    assert.equal(build.configurations[mode].assets, undefined);
  assert.equal(
    config.projects['fmheadend-ui'].architect.serve.configurations.development.buildTarget,
    'fmheadend-ui:build:development',
  );
});

for (const file of files)
  test(`production copies ${file} without modifying bytes`, async () => {
    const approved = await readFile(resolve(root, '../assets', file));
    assert.deepEqual(await readFile(resolve(root, 'tmp/branding-assets', file)), approved);
    assert.deepEqual(await readFile(resolve(output, 'assets', file)), approved);
  });

test('document title and PNG icon links use the deployment base', async () => {
  for (const path of [resolve(root, 'src/index.html'), resolve(output, 'index.html')]) {
    const html = await readFile(path, 'utf8');
    assert.match(html, /<title>FMheadend<\/title>/);
    assert.doesNotMatch(html, /favicon\.ico/);
    const base = html.match(/<base href="([^"]+)"/)[1];
    for (const size of sizes) {
      const tag = html.match(new RegExp(`<link[^>]*sizes="${size}x${size}"[^>]*>`))?.[0];
      assert.ok(tag);
      assert.match(tag, /rel="icon"/);
      assert.match(tag, /type="image\/png"/);
      const href = tag.match(/href="([^"]+)"/)[1];
      assert.equal(href, `assets/fmheadend_favicon_${size}.png?v=tower-1`);
      assert.equal(
        new URL(href, new URL(base, 'https://example.test')).pathname,
        `${base}assets/fmheadend_favicon_${size}.png`,
      );
    }
  }
});

test('logo URL resolves under a non-root base even on nested editor routes', async () => {
  const template = await readFile(resolve(root, 'src/app/layout/shell.html'), 'utf8');
  const src = template.match(/src="([^"]+fmheadend_logo_tower\.svg)"/)[1];
  assert.equal(src, `assets/${logo}`);
  for (const base of ['/', '/fmheadend/']) {
    const baseURL = new URL(base, 'https://example.test/fmheadend/audio-profiles/edit');
    assert.equal(new URL(src, baseURL).pathname, `${base}assets/${logo}`);
  }
});

test('the single self-hosted ExtraLight font is bundled with base-relative URLs', async () => {
  assert.deepEqual(
    await readFile(resolve(output, 'assets/fonts/barlow/OFL.txt')),
    await readFile(resolve(root, 'src/assets/fonts/barlow/OFL.txt')),
  );
  const html = await readFile(resolve(output, 'index.html'), 'utf8');
  const stylesheet = html.match(/href="(styles-[^"]+\.css)"/)[1];
  const css = await readFile(resolve(output, stylesheet), 'utf8');
  assert.match(css, /font-weight:200/);
  assert.match(css, /font-display:swap/);
  assert.doesNotMatch(css, /fonts\.googleapis|fonts\.gstatic/);
  const fontPath = css.match(/url\(["']?([^\)"']+Barlow-ExtraLight[^\)"']+\.woff2)["']?\)/)[1];
  const font = await readFile(resolve(output, fontPath));
  assert.equal(font.subarray(0, 4).toString(), 'wOF2');
  assert.deepEqual(
    font,
    await readFile(resolve(root, 'src/assets/fonts/barlow/Barlow-ExtraLight.woff2')),
  );
  for (const base of ['/', '/fmheadend/']) {
    const url = new URL(fontPath, `https://example.test${base}${stylesheet}`);
    assert.ok(url.pathname.startsWith(`${base}media/`));
  }
});
