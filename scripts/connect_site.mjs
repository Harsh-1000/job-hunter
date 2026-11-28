#!/usr/bin/env node
// Sign in to a job site ONCE, yourself, in a real Chrome window; save that sign-in for the bot.
//
//   node scripts/connect_site.mjs <site> <login-url> <host,host,...> <out.json>
//
// Opens Google Chrome with a fresh, throw-away profile on the site's real login page. You log in there
// (password, OTP, CAPTCHA: all by you, nothing is typed for you). Then press Enter in this terminal: only the
// cookies of that site are read over Chrome's local DevTools port and saved, in Playwright "storage state"
// format, to <out.json> (mode 0600). Chrome is closed and the temporary profile deleted.
// Your password never passes through this script or the bot.
// Needs Node 22+ (built-in WebSocket) and Google Chrome / Chromium.
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync, existsSync, mkdirSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline/promises";

const [site, loginUrl, hostsArg, out] = process.argv.slice(2);
if (!site || !loginUrl || !hostsArg || !out) {
  console.error("usage: connect_site.mjs <site> <login-url> <hosts> <out.json>");
  process.exit(64);
}
const hosts = hostsArg.split(",").filter(Boolean);
const ofSite = (domain) => {
  const d = String(domain || "").replace(/^\./, "").toLowerCase();
  return hosts.some((h) => d === h || d.endsWith("." + h));
};
const sameSite = (v) => (v === "Strict" || v === "Lax" || v === "None" ? v : "Lax");

const chrome = [process.env.CHROME, "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
  .filter(Boolean);
const profile = mkdtempSync(join(tmpdir(), "jobhunter-login-"));
const cleanup = () => rmSync(profile, { recursive: true, force: true });
const args = [`--user-data-dir=${profile}`, "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
  "--no-first-run", "--no-default-browser-check", "--new-window", loginUrl];

function launch(i = 0) {
  return new Promise((resolve, reject) => {
    if (i >= chrome.length) return reject(new Error("Google Chrome / Chromium not found (set CHROME=/path)"));
    const p = spawn(chrome[i], args, { stdio: "ignore" });
    p.once("error", () => launch(i + 1).then(resolve, reject));
    p.once("spawn", () => resolve(p));
  });
}

async function devtoolsUrl() {
  const file = join(profile, "DevToolsActivePort");
  for (let i = 0; i < 100; i++) {
    if (existsSync(file)) {
      const [port, path] = readFileSync(file, "utf8").trim().split("\n");
      if (port && path) return `ws://127.0.0.1:${port}${path}`;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("Chrome didn't open its local DevTools port");
}

function cdp(url) {
  const ws = new WebSocket(url);
  let id = 0;
  const waiting = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiting.has(m.id)) {
      const { resolve, reject } = waiting.get(m.id);
      waiting.delete(m.id);
      m.error ? reject(new Error(m.error.message)) : resolve(m.result);
    }
  };
  const ready = new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = () => reject(new Error("DevTools connection failed")); });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const n = ++id;
    waiting.set(n, { resolve, reject });
    ws.send(JSON.stringify({ id: n, method, params }));
  });
  return { ready, send, close: () => ws.close() };
}

let browser;
try {
  browser = await launch();
  const conn = cdp(await devtoolsUrl());
  await conn.ready;
  console.log(`\n🔐 A Chrome window opened on ${loginUrl}`);
  console.log("   Log in there yourself (OTP / CAPTCHA too). When you see your account page, come back here.");
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  await rl.question("   Press Enter once you're logged in (Ctrl+C to cancel)… ");
  rl.close();

  const { cookies } = await conn.send("Storage.getCookies");
  const mine = cookies.filter((c) => ofSite(c.domain)).map((c) => ({
    name: c.name, value: c.value, domain: c.domain, path: c.path,
    expires: c.session ? -1 : c.expires, httpOnly: !!c.httpOnly, secure: !!c.secure, sameSite: sameSite(c.sameSite),
  }));
  try { await conn.send("Browser.close"); } catch { /* already closing */ }
  conn.close();
  if (!mine.length) {
    console.error(`\n⚠️  No ${site} cookies found: it doesn't look like you logged in. Nothing saved.`);
    process.exitCode = 2;
  } else {
    mkdirSync(dirname(out), { recursive: true, mode: 0o700 });
    chmodSync(dirname(out), 0o700);
    const tmp = `${out}.tmp`;
    const state = { cookies: mine, origins: [], site, saved_at: new Date().toISOString() };
    writeFileSync(tmp, JSON.stringify(state), { mode: 0o600 });
    renameSync(tmp, out);
    console.log(`\n✅ Saved your ${site} sign-in (${mine.length} cookies) to ${out}`);
    console.log("   It is private to you (file mode 600). To sign out the bot: jobhunter disconnect " + site);
  }
} catch (err) {
  console.error(`\n❌ ${err.message}`);
  process.exitCode = 1;
} finally {
  if (browser && browser.exitCode === null) {
    const gone = new Promise((r) => browser.once("exit", r));
    try { browser.kill(); } catch { /* gone */ }
    await Promise.race([gone, new Promise((r) => setTimeout(r, 5000))]);
  }
  await new Promise((r) => setTimeout(r, 300));   // Chrome helpers finish writing the profile
  cleanup();
}
