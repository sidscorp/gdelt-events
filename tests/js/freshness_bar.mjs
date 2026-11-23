// Renders the REAL dashboard/static/js/markdown.js freshness bar in Node.
//
// The bar is the thing issue #71 is about, and a Python re-implementation of its
// string would test nothing: the two would drift and the test would keep passing
// while the page lied. This loads the shipped file into a vm context with a
// minimal DOM and calls updateFreshnessBar(), so the assertion is on the exact
// markup a reader sees.
//
// Usage:  node tests/js/freshness_bar.mjs '[{"generatedAt":...}, ...]'
//         -> prints a JSON array of innerHTML strings, one per state.
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

function makeEl(id) {
  const el = {
    id, innerHTML: '', textContent: '', className: '', dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    children: [],
    setAttribute() {}, removeAttribute() {}, getAttribute() { return null; },
    appendChild(c) { el.children.push(c); return c; },
    insertBefore(c) { el.children.push(c); return c; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    scrollIntoView() {},
    addEventListener() {},
    removeEventListener() {},
  };
  el.parentNode = { insertBefore() {}, appendChild() {} };
  return el;
}

function context() {
  const els = new Map();
  const get = (id) => {
    if (!els.has(id)) els.set(id, makeEl(id));
    return els.get(id);
  };
  const ctx = {
    console,
    Date, Math, JSON, Promise, String, Number, Boolean, Array, Object, RegExp, Error,
    Map, Set, AbortController, URLSearchParams, TextDecoder, TextEncoder,
    setTimeout: () => 0, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
    performance: { now: () => 0 },
    // Never settles: fetchBriefing() must suspend at its first await so no
    // network or DOM work runs at load time.
    fetch: () => new Promise(() => {}),
    document: {
      getElementById: get,
      createElement: (t) => makeEl(t),
      addEventListener: () => {},
      querySelector: get,
      querySelectorAll: () => [],
      body: makeEl('body'),
      documentElement: makeEl('html'),
    },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    navigator: { userAgent: 'node' },
    location: { search: '', href: 'http://localhost/', pathname: '/' },
    history: { replaceState: () => {}, pushState: () => {} },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  const src = fs.readFileSync(path.join(ROOT, 'dashboard', 'static', 'js', 'markdown.js'), 'utf8');
  vm.runInContext(src, ctx, { filename: 'markdown.js' });
  return { ctx, get };
}

export function render(states) {
  const out = [];
  for (const state of states) {
    // A fresh context per state: _freshnessData is module state, and reusing it
    // would let one case's ticker leak into the next.
    const { ctx, get } = context();
    vm.runInContext(
      `_freshnessData = ${JSON.stringify(state)}; updateFreshnessBar();`,
      ctx, { filename: 'harness' });
    out.push(get('briefingFreshness').innerHTML);
  }
  return out;
}

const arg = process.argv[2];
if (arg) {
  process.stdout.write(JSON.stringify(render(JSON.parse(arg))));
}
