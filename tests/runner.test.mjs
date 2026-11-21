import test from "node:test";
import assert from "node:assert/strict";
import {
	DEFAULT_SETTINGS, splitArgs, buildArgs, parseLine, statusText, augmentedPath, toVaultPath, noteToOpen,
	batchLabel, validPages, toolEnv, quoteArg, migrateSettings, parseCheck, setupProblem,
	initialState, reduce, fractionOverall, etaSeconds, formatDuration, etaLabel, statusBarText,
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
	for (const ok of ["", "  ", "7", "10-40", " 3 - 9 "]) assert.ok(validPages(ok), ok);
	for (const bad of ["0", "0-4", "9-3", "a", "1-", "-5", "1,2", "1-2-3"]) assert.ok(!validPages(bad), bad);
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
