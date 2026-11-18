// Runs background.js against a stubbed `browser` API.
// Usage: node test/background.test.mjs
import fs from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const src = fs.readFileSync(new URL("../background.js", import.meta.url), "utf8");
const store = {};
const removed = [];
const badge = { text: null, color: null };
let updatedListener, updatedFilter, startup, installed;
let openTabs = [];

const browser = {
  tabs: {
    onUpdated: { addListener: (fn, filter) => { updatedListener = fn; updatedFilter = filter; } },
    remove: async (id) => { await new Promise(r => setTimeout(r, 5)); removed.push(id); openTabs = openTabs.filter(t => t.id !== id); },
    query: async () => openTabs,
  },
  storage: { sync: {
    get: async (k) => { await new Promise(r => setTimeout(r, 1)); return k in store ? { [k]: store[k] } : {}; },
    set: async (o) => { await new Promise(r => setTimeout(r, 1)); Object.assign(store, o); },
  } },
  action: {
    setBadgeText: async ({ text }) => { badge.text = text; },
    setBadgeBackgroundColor: async ({ color }) => { badge.color = color; },
  },
  runtime: {
    onStartup: { addListener: fn => { startup = fn; } },
    onInstalled: { addListener: fn => { installed = fn; } },
  },
};
const ctx = vm.createContext({ browser, console: { log() {}, error: console.error }, URL, setTimeout, Promise });
vm.runInContext(src, ctx);
const tick = () => new Promise(r => setTimeout(r, 100));

assert.deepEqual([...updatedFilter.properties], ["url", "title", "status"]);

const t = (id, title, url) => ({ id, title, url });
// Matching: random port, both titles. Fire each 3x to exercise dedupe.
for (const tab of [t(1, "Sigstore Authentication", "http://localhost:53122/auth/callback?code=x"),
                   t(2, "Chainguard Authentication", "http://localhost:8080/"),
                   t(30, "Sigstore Authentication", "http://127.0.0.1:41234/auth/callback")]) {
  for (let i = 0; i < 3; i++) updatedListener(tab.id, {}, tab);
}
// Non-matching.
for (const tab of [t(3, "Sigstore Authentication", "https://localhost:8080/"),
                   t(4, "Sigstore Authentication", "http://example.com/"),
                   t(5, "Sigstore Authentication", "http://localhost.evil.com/"),
                   t(6, "Some other page", "http://localhost:8080/"),
                   t(7, "Sigstore Authentication", undefined),
                   t(8, undefined, "http://localhost:1/"),
                   t(31, "Sigstore Authentication", "http://127.0.0.2:8080/"),
                   t(32, "Sigstore Authentication", "https://127.0.0.1:8080/")]) {
  updatedListener(tab.id, {}, tab);
}
await tick();
assert.deepEqual(removed.sort((a, b) => a - b), [1, 2, 30], "only matching tabs closed, once each");
assert.equal(store.count, 3, "concurrent closes all counted");
assert.equal(badge.text, "3");
assert.equal(badge.color, "#221F5C");

// Startup: restores badge from storage, sweeps already-open tabs.
badge.text = null;
openTabs = [t(10, "Chainguard Authentication", "http://localhost:4444/x"), t(11, "Docs", "http://localhost:4444/")];
startup();
await tick();
assert.deepEqual(removed.sort((a, b) => a - b), [1, 2, 10, 30]);
assert.equal(store.count, 4);
assert.equal(badge.text, "4");

// A count stored as a string still increments.
store.count = "7";
updatedListener(20, {}, t(20, "Sigstore Authentication", "http://localhost:9/"));
await tick();
assert.equal(store.count, 8);
assert.ok(installed, "onInstalled registered");
console.log("harness: all assertions passed");
