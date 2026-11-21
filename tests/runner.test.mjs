import test from "node:test";
import assert from "node:assert/strict";
import {
	DEFAULT_SETTINGS, splitArgs, buildArgs, parseLine, statusText, augmentedPath, toVaultPath, noteToOpen,
	batchLabel, validPages, toolEnv, quoteArg, migrateSettings, parseCheck, setupProblem,
	parseEstimate, buildInfoArgs, estimateText, needsConfirmation,
	initialState, reduce, fractionOverall, etaSeconds, formatDuration, etaLabel, statusBarText,
	updateCommand, versionWarning, checkOutputFolder, parseResult, needsReview, reviewItems, retryOutput, pickPdf,
	dirName, baseName, retryPages,
	parsePageList, mergeRanges, parseOutline, buildOutlineArgs, selectionToPages, toggleEntry, checkState, descendants,
	childrenOf, parentOf, estimateSeconds, rowLabel, totalLabel, enrichmentSummary,
} from "../.test-build/runner.mjs";

test("splitArgs honours quotes and empty input", () => {
	assert.deepEqual(splitArgs(""), []);
	assert.deepEqual(splitArgs("   "), []);
	assert.deepEqual(splitArgs("--a 1 --b 'two words' \"three words\""), ["--a", "1", "--b", "two words", "three words"]);
	assert.deepEqual(splitArgs('--x ""'), ["--x", ""]);
});

test("buildArgs: defaults call pdf2md with the Obsidian flavour and the Paddle server", () => {
	const a = buildArgs("/v/a.pdf", DEFAULT_SETTINGS);
	assert.equal(a[0], "/v/a.pdf");
	assert.equal(a[a.indexOf("--flavor") + 1], "obsidian");
	assert.equal(a[a.indexOf("--paddle-server") + 1], "http://127.0.0.1:8111/v1");
	assert.equal(a[a.indexOf("--paddle-scale") + 1], "2.5");
	assert.equal(a[a.indexOf("--serve-command") + 1], "pdf2md serve");
	assert.equal(a[a.indexOf("--page-links") + 1], "auto");
	for (const gone of ["--engine", "--model", "--host", "--jobs", "--api", "--clean", "--no-llm", "--split-chapters", "-o", "--pages"]) {
		assert.ok(!a.includes(gone), gone);
	}
});

test("buildArgs: toggles, output folder, scale, server, pages and extra args", () => {
	const s = { ...DEFAULT_SETTINGS, splitChapters: true, paddleScale: 9, paddleServer: " http://h:1/v1 ", pageLinks: "off",
		serveCommand: " /x/serve.sh ", pdf2mdPath: "/opt/my tools/pdf2md", extraArgs: "--no-cache --serve-wait 300" };
	const a = buildArgs("/v/a.pdf", s, "/v/out", "10-40");
	assert.ok(a.includes("--split-chapters"));
	assert.equal(a[a.indexOf("-o") + 1], "/v/out");
	assert.equal(a[a.indexOf("--pages") + 1], "10-40");
	assert.equal(a[a.indexOf("--paddle-scale") + 1], "4");               // clamped
	assert.equal(a[a.indexOf("--paddle-server") + 1], "http://h:1/v1");
	assert.equal(a[a.indexOf("--serve-command") + 1], "/x/serve.sh");
	assert.equal(a[a.indexOf("--page-links") + 1], "off");
	assert.deepEqual(a.slice(-3), ["--no-cache", "--serve-wait", "300"]);
	const bare = buildArgs("/v/a.pdf", { ...DEFAULT_SETTINGS, paddleServer: " ", pdf2mdPath: "/opt/my tools/pdf2md" });
	assert.equal(bare[bare.indexOf("--paddle-server") + 1], "http://127.0.0.1:8111/v1");
	assert.equal(bare[bare.indexOf("--serve-command") + 1], '"/opt/my tools/pdf2md" serve');   // a path with spaces stays one word
});

test("buildArgs: hidden page markers are off unless asked for", () => {
	assert.ok(!buildArgs("/v/a.pdf", DEFAULT_SETTINGS).includes("--page-markers"));
	assert.ok(buildArgs("/v/a.pdf", { ...DEFAULT_SETTINGS, keepPageMarkers: true }).includes("--page-markers"));
});

test("quoteArg", () => {
	assert.equal(quoteArg("pdf2md"), "pdf2md");
	assert.equal(quoteArg("/a b/c"), '"/a b/c"');
	assert.equal(quoteArg('a"b'), '"a\\"b"');
});

test("buildArgs: several PDFs go first, in order", () => {
	const a = buildArgs(["/v/a.pdf", "/v/b.pdf", "/v/c.pdf"], DEFAULT_SETTINGS, "/v/out");
	assert.deepEqual(a.slice(0, 5), ["/v/a.pdf", "/v/b.pdf", "/v/c.pdf", "-o", "/v/out"]);
});

test("migrateSettings keeps known values and drops everything else", () => {
	const m = migrateSettings({ engine: "docling", textModel: "gemma4:12b", apiKey: "SECRET", paddleScale: 4, splitChapters: true,
		outputFolder: "Imports", paddleServer: 7, pageLinks: "on" });
	assert.equal(m.paddleScale, 4);
	assert.equal(m.splitChapters, true);
	assert.equal(m.outputFolder, "Imports");
	assert.equal(m.pageLinks, "on");
	assert.equal(m.paddleServer, DEFAULT_SETTINGS.paddleServer);          // wrong type: default
	assert.deepEqual(Object.keys(m).sort(), Object.keys(DEFAULT_SETTINGS).sort());
	assert.equal(JSON.stringify(m).includes("SECRET"), false);
	assert.deepEqual(migrateSettings(null), DEFAULT_SETTINGS);
});

test("setup check: parsing and what the user is told", () => {
	const ok = { ready: true, checks: [
		{ id: "worker", ok: true, required: true, detail: "worker", fix: "pdf2md setup" },
		{ id: "llama-server", ok: false, required: false, detail: "llama", fix: "install llama.cpp" },
		{ id: "server", ok: false, required: false, detail: "server", fix: "pdf2md serve" } ] };
	assert.equal(parseCheck("not json"), null);
	assert.equal(parseCheck('{"ready": 1}'), null);
	assert.deepEqual(parseCheck(JSON.stringify(ok)), ok);
	assert.match(setupProblem(ok, false), /llama-server was not found/);       // nothing can start a server
	assert.equal(setupProblem(ok, true), null);                                // a start command is configured
	const down = { ready: false, checks: [{ id: "worker", ok: false, required: true, detail: "worker environment", fix: "pdf2md setup" }] };
	assert.match(setupProblem(down, true), /pdf2md setup/);
	const serving = { ready: true, checks: [{ id: "server", ok: true, required: false, detail: "s", fix: "" }] };
	assert.equal(setupProblem(serving, false), null);
});

test("parseLine understands the tool's real output", () => {
	assert.deepEqual(parseLine("  slice 8/14 (pages 281-320): 73 equation(s), 38/43 inline [1620s, ETA ~20 min]"),
		{ kind: "slice", n: 8, total: 14, etaMin: 20 });
	assert.deepEqual(parseLine("  slice 14/14 (pages 521-552): 51 equation(s) [2718s]"), { kind: "slice", n: 14, total: 14, etaMin: undefined });
	assert.deepEqual(parseLine("[4/4] wrote /v/Books/My Book.md (2719s)"), { kind: "wrote", path: "/v/Books/My Book.md", split: false });
	assert.deepEqual(parseLine("[4/4] wrote 16 notes + index to /v/Books/My Book (2862s)"),
		{ kind: "wrote", path: "/v/Books/My Book", split: true, notes: 16 });
	assert.deepEqual(parseLine("[3/4] math check: 0 problem(s)"), { kind: "stage", stage: 3, text: "math check: 0 problem(s)" });
	assert.equal(parseLine("error: missing worker environment. Run `pdf2md setup` once").kind, "error");
	assert.equal(parseLine("some warning").kind, "other");
	assert.equal(parseLine("").kind, "other");
});

test("statusText", () => {
	assert.equal(statusText({ kind: "slice", n: 2, total: 14, etaMin: 30 }), "PDF→MD: slice 2/14 · ~30 min left");
	assert.equal(statusText({ kind: "slice", n: 14, total: 14 }), "PDF→MD: slice 14/14");
	assert.equal(statusText({ kind: "other" }), null);
});

test("augmentedPath adds the usual tool locations once", () => {
	const p = augmentedPath({ PATH: "/usr/bin:/home/u/.local/bin" }, "/home/u");
	const parts = p.split(":");
	assert.equal(parts.filter((x) => x === "/home/u/.local/bin").length, 1);
	assert.ok(parts.includes("/home/u/.cargo/bin") && parts.includes("/opt/homebrew/bin") && parts[0] === "/usr/bin");
	assert.ok(augmentedPath({}, "/h").startsWith("/h/.local/bin"));
});

test("toVaultPath and noteToOpen", () => {
	assert.equal(toVaultPath("/v", "/v/Books/a.md"), "Books/a.md");
	assert.equal(toVaultPath("/v/", "/v/Books/a.md"), "Books/a.md");
	assert.equal(toVaultPath("C:\\Vault", "C:\\Vault\\Books\\a.md"), "Books/a.md");
	assert.equal(toVaultPath("/v", "/other/a.md"), null);
	assert.equal(toVaultPath("/v", "/vault2/a.md"), null);          // shares a prefix, is not inside
	assert.equal(noteToOpen({ path: "Books/My Book", split: true }), "Books/My Book/00 Index.md");
	assert.equal(noteToOpen({ path: "Books/a.md", split: false }), "Books/a.md");
});

test("parseLine and statusText understand batches", () => {
	assert.deepEqual(parseLine("=== PDF 2/5: Some Book (2020).pdf"), { kind: "pdf", n: 2, total: 5, name: "Some Book (2020).pdf" });
	assert.equal(statusText({ kind: "pdf", n: 2, total: 5, name: "x" }), "PDF→MD: PDF 2/5");
	assert.equal(statusText({ kind: "slice", n: 3, total: 14, etaMin: 9 }, "PDF 2/5"), "PDF→MD: PDF 2/5 · slice 3/14 · ~9 min left");
	assert.equal(batchLabel({ kind: "pdf", n: 2, total: 5, name: "x" }), "PDF 2/5");
	assert.equal(batchLabel({ kind: "pdf", n: 1, total: 1, name: "x" }), "");
	assert.equal(batchLabel(null), "");
	assert.equal(batchLabel({ kind: "other" }), "");
});

test("validPages accepts the tool's page-range syntax only", () => {
	for (const ok of ["", "  ", "7", "10-40", " 3 - 9 ", "10-40,60-80,95", "5, 1-2 ,9"]) assert.ok(validPages(ok), ok);
	for (const bad of ["0", "0-4", "9-3", "a", "1-", "-5", "1,", ",2", "1,,2", "3,1-0", "1-2-3"]) assert.ok(!validPages(bad), bad);
});

test("toolEnv extends PATH without losing the rest", () => {
	const e = toolEnv({ PATH: "/usr/bin", HOME: "/h", EMPTY: undefined }, "/h");
	assert.equal(e.HOME, "/h");
	assert.ok(e.PATH.includes("/h/.local/bin") && e.PATH.startsWith("/usr/bin"));
	assert.equal(e.PYTHONUNBUFFERED, "1");
	assert.ok(!("EMPTY" in e));
});

const T0 = 1_000_000;
const feed = (state, lines, t) => lines.reduce((st, l) => reduce(st, parseLine(l), t), state);

test("parseLine reads the tool's PROGRESS lines", () => {
	assert.deepEqual(parseLine("PROGRESS 120 552"), { kind: "pages", done: 120, total: 552 });
	assert.equal(parseLine("PROGRESS x y").kind, "other");
});

test("a single conversion: percentage, stage and ETA follow the pages", () => {
	let s = initialState(T0, 1);
	assert.equal(etaSeconds(s, T0), null);
	assert.equal(etaLabel(s, T0), "estimating…");
	s = feed(s, ["PROGRESS 0 100"], T0);
	assert.equal(fractionOverall(s), 0);
	assert.equal(etaSeconds(s, T0), null, "one sample is not enough");
	s = feed(s, ["PROGRESS 10 100"], T0 + 40_000);                     // 10 pages in 40 s
	assert.equal(fractionOverall(s), 0.1);
	assert.equal(s.stage, "Reading pages");
	assert.equal(Math.round(etaSeconds(s, T0 + 40_000)), 360);         // 90 pages left at 0.25 pages/s
	assert.equal(statusBarText(s, T0 + 40_000), "PDF→MD: 10% · ~6 min");
	s = feed(s, ["PROGRESS 100 100"], T0 + 400_000);
	assert.equal(s.stage, "Finishing up");
	assert.equal(etaSeconds(s, T0 + 400_000), 0);
	assert.equal(etaLabel(s, T0 + 400_000), "almost done");
	s = feed(s, ["[3/4] math check: 0 problem(s)"], T0 + 410_000);
	assert.equal(s.stage, "Checking the maths");
	s = feed(s, ["[4/4] wrote /v/a.md (400s)"], T0 + 420_000);
	assert.equal(s.stage, "Saved");
	assert.equal(s.wrote, 1);
});

test("a resumed run does not look faster than it is", () => {
	let s = initialState(T0, 1);
	s = feed(s, ["PROGRESS 0 200"], T0);
	s = feed(s, ["PROGRESS 120 200"], T0 + 2_000);                     // 120 cached pages skipped instantly
	const early = etaSeconds(s, T0 + 2_000);
	assert.ok(early !== null && early < 5, "right after the jump the window still holds it");
	s = feed(s, ["PROGRESS 130 200"], T0 + 400_000);                   // later: real pace, 10 pages in ~6 min
	const later = etaSeconds(s, T0 + 400_000);
	assert.ok(later > 1500, `the old jump must age out of the estimate (got ${later}s)`);
});

test("a batch: overall percentage, per-PDF reset, ETA from finished PDFs", () => {
	let s = initialState(T0, 3);
	s = feed(s, ["=== PDF 1/3: a.pdf", "PROGRESS 0 10"], T0);
	s = feed(s, ["PROGRESS 10 10", "[4/4] wrote /v/a.md (100s)"], T0 + 100_000);
	assert.deepEqual(s.finishedMs, [100_000]);
	s = feed(s, ["=== PDF 2/3: b.pdf", "PROGRESS 0 20"], T0 + 101_000);
	assert.equal(s.pagesDone, 0);
	assert.equal(s.pdfName, "b.pdf");
	s = feed(s, ["PROGRESS 10 20"], T0 + 151_000);                     // half of b in 50 s
	assert.equal(fractionOverall(s).toFixed(3), "0.500");              // (1 finished + 0.5) / 3
	assert.equal(Math.round(etaSeconds(s, T0 + 151_000)), 150);        // 50 s for the rest of b + one PDF averaging 100 s
	assert.equal(statusBarText(s, T0 + 151_000), "PDF→MD: PDF 2/3 · 50% · ~3 min");
	assert.equal(fractionOverall(feed(s, ["PROGRESS 20 20"], T0 + 200_000)).toFixed(3), "0.667");
});

test("errors are remembered and unknown lines change nothing", () => {
	const s = initialState(T0, 1);
	assert.equal(reduce(s, parseLine("some warning"), T0), s);
	assert.equal(feed(s, ["error: cannot reach the server"], T0).lastError, "error: cannot reach the server");
});

test("formatDuration and the label read naturally", () => {
	assert.equal(formatDuration(3), "3 s");
	assert.equal(formatDuration(0.2), "1 s");
	assert.equal(formatDuration(59.6), "60 s");
	assert.equal(formatDuration(61), "1 min");
	assert.equal(formatDuration(600), "10 min");
	assert.equal(formatDuration(5399), "90 min");
	assert.equal(formatDuration(5400), "1 h 30 min");
	assert.equal(formatDuration(7230), "2 h 1 min");
	assert.equal(formatDuration(7199), "2 h");
});

test("estimate: parsing, text and when to ask", () => {
	const raw = { files: [{ name: "a.pdf", pages: 541, seconds: 1854 }], pages: 541, seconds: 1854, unknown: 0, sec_per_page: 3.4, measured: false };
	assert.equal(parseEstimate("nope"), null);
	assert.equal(parseEstimate("{}"), null);
	const e = parseEstimate(JSON.stringify(raw));
	assert.equal(e.pages, 541);
	assert.equal(estimateText(e, false), "541 pages: about 31 min (a typical GPU speed). The model server will be started first.");
	assert.match(estimateText({ ...e, measured: true, pages: 1, seconds: 20 }, true), /^1 page: about 20 s \(from your recent conversions\)\. The model server is already running\.$/);
	assert.match(estimateText({ ...e, unknown: 2 }, true), /plus 2 PDFs of unknown length/);
	assert.ok(needsConfirmation(e, 5));
	assert.ok(!needsConfirmation({ ...e, seconds: 120 }, 5));
	assert.ok(!needsConfirmation(e, 0), "0 means never ask");
	assert.equal(DEFAULT_SETTINGS.confirmOverMinutes, 5);
});

test("buildInfoArgs passes the page range only for a single PDF", () => {
	assert.deepEqual(buildInfoArgs(["/a.pdf"], " 10-40 "), ["info", "--json", "/a.pdf", "--pages", "10-40"]);
	assert.deepEqual(buildInfoArgs(["/a.pdf", "/b.pdf"], "10-40"), ["info", "--json", "/a.pdf", "/b.pdf"]);
	assert.deepEqual(buildInfoArgs(["/a.pdf"]), ["info", "--json", "/a.pdf"]);
});

test("toVaultPath also matches the real path of a symlinked vault", () => {
	assert.equal(toVaultPath("/home/u/link", "/data/vault/Books/a.md", "/data/vault"), "Books/a.md");
	assert.equal(toVaultPath("/home/u/link", "/home/u/link/Books/a.md", "/data/vault"), "Books/a.md");
	assert.equal(toVaultPath("/home/u/link", "/data/vault2/a.md", "/data/vault"), null);
	assert.equal(toVaultPath("/home/u/link", "/data/vault/a.md", null), null);
});

test("parseCheck keeps the version; versionWarning compares major.minor", () => {
	assert.equal(parseCheck('{"ready":true,"checks":[],"version":"0.6.2"}').version, "0.6.2");
	assert.ok(!("version" in parseCheck('{"ready":true,"checks":[]}')));
	assert.equal(versionWarning("0.6.2", "0.6.0"), null);
	assert.match(versionWarning(undefined, "0.6.0"), /does not report its version/);
	assert.match(versionWarning("0.5.0", "0.6.0"), /0\.5\.0.*0\.6\.0/);
	assert.match(versionWarning("1.6.0", "0.6.0"), /1\.6\.0/);
	assert.equal(updateCommand("0.6.0"), "uv tool install --force --reinstall git+https://github.com/jyothisable/pdf2md-local@0.6.0");
});

test("checkOutputFolder", () => {
	assert.deepEqual(checkOutputFolder("  ", ".obsidian"), { ok: true, folder: "" });
	assert.deepEqual(checkOutputFolder("Imports/./Books/", ".obsidian"), { ok: true, folder: "Imports/Books" });
	assert.deepEqual(checkOutputFolder("Imports\\Books", ".obsidian"), { ok: true, folder: "Imports/Books" });
	for (const bad of ["../x", "a/../../x", "/abs", "\\\\server\\share", "C:\\Users", "C:foo", "d:/x", ".obsidian", ".obsidian/plugins/x", ".Obsidian/x"]) {
		assert.equal(checkOutputFolder(bad, ".obsidian").ok, false, bad);
	}
	assert.equal(checkOutputFolder(".obsidianx/a", ".obsidian").ok, true);
	assert.equal(checkOutputFolder(".config/a", ".config").ok, false);
});

test("parseResult reads RESULT lines and ignores malformed ones", () => {
	const good = { pdf: "/v/a.pdf", output: "/v/a.md", folder: null, notes: ["/v/a.md"], pages_failed: [40, 12],
		formula_images: [{ page: 7, note: "/v/a.md" }, { page: "x", note: "/v/a.md" }],
		math_warnings: [{ note: "/v/a.md", count: 3 }, { note: "/v/b.md", count: 0 }], cache_kept: true };
	const r = parseResult("RESULT " + JSON.stringify(good));
	assert.deepEqual(r, { pdf: "/v/a.pdf", output: "/v/a.md", folder: null, notes: ["/v/a.md"], pagesFailed: [12, 40],
		formulaImages: [{ page: 7, note: "/v/a.md" }], mathWarnings: [{ note: "/v/a.md", count: 3 }], cacheKept: true });
	assert.equal(parseResult("RESULT {nope"), null);
	assert.equal(parseResult("RESULT []"), null);
	assert.equal(parseResult('RESULT {"output":"/x"}'), null);
	assert.equal(parseResult("[4/4] wrote /v/a.md (3s)"), null);
	const min = parseResult('  RESULT {"pdf":"/a.pdf","output":"/a.md"}  ');
	assert.deepEqual(min.pagesFailed, []);
	assert.equal(min.cacheKept, false);
	assert.equal(needsReview(min), false);
	assert.deepEqual(reviewItems([min, r]), [r]);
	assert.deepEqual(reviewItems([min]), []);
});

test("retryOutput, dirName, baseName and pickPdf", () => {
	assert.deepEqual(retryOutput("/v/B/a.md", null), { trash: "/v/B/a.md", out: "/v/B/a.md" });
	assert.deepEqual(retryOutput("/v/B/Book/00 Index.md", "/v/B/Book"), { trash: "/v/B/Book", out: "/v/B" });
	assert.equal(dirName("C:\\v\\Book"), "C:\\v");
	assert.equal(dirName("/Book"), "/");
	assert.equal(baseName("/v/B/a.md"), "a.md");
	assert.deepEqual(pickPdf("Notes/a.md", []), { pick: null, choices: [] });
	assert.deepEqual(pickPdf("Notes/a.md", ["x/a.pdf"]), { pick: "x/a.pdf", choices: [] });
	assert.deepEqual(pickPdf("Notes/Book/a.md", ["x/a.pdf", "Notes/a.pdf"]), { pick: "Notes/a.pdf", choices: [] });
	assert.deepEqual(pickPdf("Notes/a.md", ["x/a.pdf", "y/a.pdf"]), { pick: null, choices: ["x/a.pdf", "y/a.pdf"] });
});

test("retryPages feeds the original page range into the retry arguments", () => {
	assert.equal(retryPages("10-40"), "10-40");
	assert.equal(retryPages(" 10 - 40 "), "10-40");
	assert.equal(retryPages(7), "7");
	for (const none of [undefined, null, "", "  ", "9-3", "x", "1,", "5,3-1", []]) assert.equal(retryPages(none), undefined, String(none));
	const a = buildArgs("/v/a.pdf", DEFAULT_SETTINGS, "/v/B/a.md", retryPages("10-40"));
	assert.equal(a[a.indexOf("--pages") + 1], "10-40");
	assert.equal(a[a.indexOf("-o") + 1], "/v/B/a.md");
	assert.ok(!buildArgs("/v/a.pdf", DEFAULT_SETTINGS, "/v/B/a.md", retryPages(undefined)).includes("--pages"));
});

test("retryPages accepts and normalises the list syntax", () => {
	assert.equal(retryPages("10-40, 60-80 ,95"), "10-40,60-80,95");
	assert.equal(retryPages("60-80,10-40,41-50"), "10-50,60-80");
	assert.equal(retryPages("95"), "95");
});

test("buildArgs passes callouts and cross-links only when not auto", () => {
	const a = buildArgs("/v/a.pdf", DEFAULT_SETTINGS);
	assert.ok(!a.includes("--callouts") && !a.includes("--cross-links"));
	const b = buildArgs("/v/a.pdf", { ...DEFAULT_SETTINGS, callouts: "off", crossLinks: "on" });
	assert.equal(b[b.indexOf("--callouts") + 1], "off");
	assert.equal(b[b.indexOf("--cross-links") + 1], "on");
});

test("migrateSettings: new options default to auto and reject bad values", () => {
	assert.equal(migrateSettings({ splitChapters: true }).callouts, "auto");
	assert.equal(migrateSettings({}).crossLinks, "auto");
	assert.equal(migrateSettings({ callouts: "on", crossLinks: "off" }).callouts, "on");
	assert.equal(migrateSettings({ callouts: "maybe" }).callouts, "auto");
});

test("parseResult tolerates missing callouts/cross_links and reads them when present", () => {
	const old = parseResult('RESULT {"pdf":"/a.pdf","output":"/a.md"}');
	assert.equal(old.callouts, undefined);
	assert.equal(enrichmentSummary(old), null);
	const r = parseResult('RESULT {"pdf":"/a.pdf","output":"/a.md","callouts":12,"cross_links":48}');
	assert.equal(enrichmentSummary(r), "12 callouts, 48 cross-reference links");
	const one = parseResult('RESULT {"pdf":"/a.pdf","output":"/a.md","callouts":1,"cross_links":0}');
	assert.equal(enrichmentSummary(one), "1 callout, 0 cross-reference links");
	assert.equal(enrichmentSummary(parseResult('RESULT {"pdf":"/a.pdf","output":"/a.md","callouts":0,"cross_links":0}')), null);
});

const OUT = { pages: 100, sec_per_page: 3, measured: false, entries: [
	{ title: "Part I", level: 1, first: 1, last: 50 },
	{ title: "1 Intro", level: 2, first: 5, last: 20 },
	{ title: "1.1 Scope", level: 3, first: 5, last: 10 },
	{ title: "1.2 Method", level: 3, first: 11, last: 20 },
	{ title: "2 Basics", level: 2, first: 21, last: 50 },
	{ title: "Part II", level: 1, first: 51, last: 100 },
] };

test("outline: args and parsing", () => {
	assert.deepEqual(buildOutlineArgs("/v/a.pdf"), ["outline", "--json", "/v/a.pdf"]);
	const o = parseOutline(JSON.stringify(OUT));
	assert.equal(o.pages, 100);
	assert.equal(o.secPerPage, 3);
	assert.equal(o.entries.length, 6);
	assert.deepEqual(parseOutline('{"pages":5,"sec_per_page":3,"measured":true,"entries":[]}').entries, []);
	for (const bad of ["nope", "[]", "{}", '{"pages":5,"sec_per_page":3,"entries":[{"title":"x","level":0,"first":1,"last":2}]}',
		'{"pages":5,"sec_per_page":3,"entries":[{"title":"x","level":1,"first":3,"last":2}]}',
		'{"pages":5,"entries":[]}']) assert.equal(parseOutline(bad), null, bad);
});

test("outline tree helpers", () => {
	const e = OUT.entries.map((x) => ({ ...x }));
	assert.deepEqual(descendants(e, 0), [1, 2, 3, 4]);
	assert.deepEqual(childrenOf(e, 0), [1, 4]);
	assert.deepEqual(childrenOf(e, 5), []);
	assert.equal(parentOf(e, 3), 1);
	assert.equal(parentOf(e, 5), -1);
	let sel = toggleEntry(e, new Set(), 1, true);
	assert.deepEqual([...sel].sort(), [1, 2, 3]);
	assert.equal(checkState(e, sel, 0), "mixed");
	assert.equal(checkState(e, sel, 1), "checked");
	assert.equal(checkState(e, sel, 4), "unchecked");
	sel = toggleEntry(e, sel, 4, true);                  // all children of Part I now selected: parent follows
	assert.equal(checkState(e, sel, 0), "checked");
	sel = toggleEntry(e, sel, 2, false);                 // unchecking a leaf clears its ancestors
	assert.equal(checkState(e, sel, 1), "mixed");
	assert.equal(checkState(e, sel, 0), "mixed");
	assert.equal(checkState(e, toggleEntry(e, sel, 0, false), 3), "unchecked");
});

test("selectionToPages merges like the tool", () => {
	const e = OUT.entries;
	assert.equal(selectionToPages(e, []), "");
	assert.equal(selectionToPages(e, [2, 3]), "5-20");                 // adjacent ranges merge
	assert.equal(selectionToPages(e, [5, 2]), "5-10,51-100");
	assert.equal(selectionToPages(e, [0, 1]), "1-50");                 // overlap
	const single = [{ title: "x", level: 1, first: 95, last: 95 }, { title: "y", level: 1, first: 10, last: 40 }, { title: "z", level: 1, first: 60, last: 80 }];
	assert.equal(selectionToPages(single, [0, 1, 2]), "10-40,60-80,95");
	assert.equal(selectionToPages(single, [0]), "95");
	assert.deepEqual(mergeRanges([[5, 6], [1, 2], [3, 4]]), [[1, 6]]);
	assert.deepEqual(parsePageList("2, 4-5"), [[2, 2], [4, 5]]);
});

test("estimates for the picker", () => {
	assert.equal(estimateSeconds(10, 3.4), 34);
	assert.equal(rowLabel({ title: "t", level: 1, first: 13, last: 28 }, 3.75), "pp. 13–28 · 1 min");
	assert.equal(rowLabel({ title: "t", level: 1, first: 4, last: 4 }, 3), "p. 4 · 3 s");
	assert.equal(totalLabel([], 3), "Nothing selected");
	assert.equal(totalLabel([[1, 20], [30, 40]], 3), "31 pages · about 2 min");
});

test("enrichmentSummary includes the PDF's own links and web links", () => {
	const r = parseResult('RESULT {"pdf":"/a.pdf","output":"/a.md","callouts":12,"cross_links":48,"pdf_links":30,"url_links":5}');
	assert.equal(r.pdfLinks, 30);
	assert.equal(r.urlLinks, 5);
	assert.equal(enrichmentSummary(r), "12 callouts, 48 cross-reference links (30 from the PDF's own links), 5 web links");
	const one = parseResult('RESULT {"pdf":"/a.pdf","output":"/a.md","callouts":0,"cross_links":1,"pdf_links":0,"url_links":1}');
	assert.equal(enrichmentSummary(one), "0 callouts, 1 cross-reference link, 1 web link");
	assert.equal(enrichmentSummary(parseResult('RESULT {"pdf":"/a.pdf","output":"/a.md","url_links":2}')), "2 web links");
	const bad = parseResult('RESULT {"pdf":"/a.pdf","output":"/a.md","pdf_links":"x","url_links":-1}');
	assert.equal(bad.pdfLinks, undefined);
	assert.equal(bad.urlLinks, undefined);
});
