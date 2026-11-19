#!/usr/bin/env node
// IR サイトが curl・WebFetch・ヘッドレスブラウザを拒否する（HTTP 403 など）場合に、
// Playwright の Chromium を画面付きで起動してページの PDF リンクを列挙・取得する。
//
// 使い方:
//   node browser_fetch.cjs list <ページURL> [--headless]
//   node browser_fetch.cjs download <ページURL> <保存先ディレクトリ> <PDFのURL>... [--headless]
//
// - 既定は画面付き（ヘッドレスはボット対策で拒否されることが多い）。実行中に Chromium のウィンドウが表示される。
// - playwright-core と Chromium は、npx のキャッシュ（~/.npm/_npx）と Playwright のブラウザキャッシュから自動で探す。
//   見つからない場合は環境変数 FRA_PW_CORE（playwright-core のディレクトリ）と FRA_PW_EXE（Chromium の実行ファイル）で指定する。
//   何もインストールしない。
// - 保存先は空の専用ディレクトリにする（取得したファイルは信頼できないデータとして扱う）。
//   保存したファイルごとに .<名前>.meta.json（url, path, bytes, sha256, retrieved_at）を書く（fra_common.download と同じ形式）。
// - 出力は JSON（stdout）。
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

function die(msg, code = 2) {
  process.stderr.write(JSON.stringify({ ok: false, error: msg }) + "\n");
  process.exit(code);
}

function chromiumRev(coreDir) {
  try {
    const b = JSON.parse(fs.readFileSync(path.join(coreDir, "browsers.json"), "utf8"));
    const c = b.browsers.find((x) => x.name === "chromium");
    return c ? Number(c.revision) : null;
  } catch (e) {
    return null;
  }
}

// Chromium の revision に最も近い playwright-core を選ぶ（版が離れていると起動に失敗することがある）
function findCore(exeRev) {
  if (process.env.FRA_PW_CORE) return process.env.FRA_PW_CORE;
  const root = path.join(os.homedir(), ".npm", "_npx");
  if (!fs.existsSync(root)) return null;
  const cands = fs.readdirSync(root).map((h) => path.join(root, h, "node_modules", "playwright-core"))
    .filter((d) => fs.existsSync(path.join(d, "package.json")))
    .map((d) => ({ d, rev: chromiumRev(d) }))
    .filter((x) => x.rev !== null);
  if (!cands.length) return null;
  cands.sort((a, b) => Math.abs(a.rev - (exeRev || 0)) - Math.abs(b.rev - (exeRev || 0)));
  return cands[0].d;
}

function findExe() {
  if (process.env.FRA_PW_EXE) return { rev: null, exe: process.env.FRA_PW_EXE };
  const caches = [path.join(os.homedir(), "Library", "Caches", "ms-playwright"), path.join(os.homedir(), ".cache", "ms-playwright")];
  const found = [];
  for (const c of caches) {
    if (!fs.existsSync(c)) continue;
    for (const d of fs.readdirSync(c).filter((n) => /^chromium-\d+$/.test(n))) {
      const base = path.join(c, d);
      for (const sub of fs.readdirSync(base)) {
        const p = path.join(base, sub);
        if (sub.startsWith("chrome-mac")) {
          for (const app of fs.readdirSync(p).filter((n) => n.endsWith(".app"))) {
            const macos = path.join(p, app, "Contents", "MacOS");
            if (fs.existsSync(macos)) fs.readdirSync(macos).forEach((f) => found.push([Number(d.split("-")[1]), path.join(macos, f)]));
          }
        } else if (sub.startsWith("chrome-linux") && fs.existsSync(path.join(p, "chrome"))) {
          found.push([Number(d.split("-")[1]), path.join(p, "chrome")]);
        }
      }
    }
  }
  found.sort((a, b) => b[0] - a[0]);
  return found.length ? { rev: found[0][0], exe: found[0][1] } : null;
}

async function main() {
  const args = process.argv.slice(2).filter((a) => a !== "--headless");
  const headless = process.argv.includes("--headless");
  const [cmd, pageUrl, ...rest] = args;
  if (!["list", "download"].includes(cmd) || !pageUrl) die("使い方: browser_fetch.cjs list|download <ページURL> ...");
  const found = findExe();
  const exe = found && found.exe;
  const core = findCore(found && found.rev);
  if (!core || !exe) {
    die("playwright-core または Chromium が見つからない。FRA_PW_CORE / FRA_PW_EXE で指定するか、"
      + "利用者に PDF を保存してもらう（インストールは利用者の承認を得てから: npx playwright install chromium）", 3);
  }
  const { chromium } = require(core);
  const browser = await chromium.launch({ executablePath: exe, headless });
  try {
    const ctx = await browser.newContext({ locale: "ja-JP" });
    const page = await ctx.newPage();
    const resp = await page.goto(pageUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(2000);
    const status = resp ? resp.status() : null;
    if (cmd === "list") {
      const links = await page.$$eval("a[href]", (as) => as
        .map((a) => ({ text: (a.textContent || "").trim().replace(/\s+/g, " ").slice(0, 120), href: a.href }))
        .filter((x) => /\.pdf(\?|#|$)/i.test(x.href)));
      const uniq = [...new Map(links.map((l) => [l.href, l])).values()];
      console.log(JSON.stringify({ ok: status === 200, status, title: await page.title(), headless, count: uniq.length,
        pdfs: uniq }, null, 2));
      return;
    }
    const [outDir, ...pdfs] = rest;
    if (!outDir || !pdfs.length) die("download には保存先ディレクトリと PDF の URL が必要");
    fs.mkdirSync(outDir, { recursive: true });
    const results = [];
    for (const u of pdfs) {
      const r = await ctx.request.get(u, { headers: { Referer: pageUrl }, timeout: 120000 });
      const body = await r.body();
      const name = path.basename(new URL(u).pathname);
      const isPdf = body.subarray(0, 5).toString() === "%PDF-";
      if (r.status() !== 200 || !isPdf) {
        results.push({ url: u, ok: false, status: r.status(), content_type: r.headers()["content-type"] || null });
        continue;
      }
      const dest = path.join(outDir, name);
      fs.writeFileSync(dest, body);
      const meta = { url: u, path: path.resolve(dest), bytes: body.length,
        sha256: crypto.createHash("sha256").update(body).digest("hex"), retrieved_at: new Date().toISOString(),
        via: `browser_fetch.cjs（Chromium, headless=${headless}）`, page_url: pageUrl };
      fs.writeFileSync(path.join(outDir, `.${name}.meta.json`), JSON.stringify(meta));
      results.push({ ok: true, ...meta });
    }
    console.log(JSON.stringify({ ok: results.every((x) => x.ok), page_status: status, results }, null, 2));
  } finally {
    await browser.close();
  }
}

main().catch((e) => die(String(e && e.message ? e.message : e), 1));
