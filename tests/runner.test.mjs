import test from "node:test";
import assert from "node:assert/strict";
import {
	DEFAULT_SETTINGS, splitArgs, buildArgs, parseLine, statusText, augmentedPath, toVaultPath, noteToOpen, clampInt,
} from "../.test-build/runner.mjs";

test("splitArgs honours quotes and empty input", () => {
	assert.deepEqual(splitArgs(""), []);
	assert.deepEqual(splitArgs("   "), []);
	assert.deepEqual(splitArgs("--a 1 --b 'two words' \"three words\""), ["--a", "1", "--b", "two words", "three words"]);
	assert.deepEqual(splitArgs('--x ""'), ["--x", ""]);
});

test("buildArgs: defaults run the bundled script with the Ollama options", () => {
	const a = buildArgs("/s/pdf_to_md.py", "/v/a.pdf", DEFAULT_SETTINGS);
	assert.deepEqual(a.slice(0, 5), ["run", "--python", "3.12", "/s/pdf_to_md.py", "/v/a.pdf"]);
	for (const pair of [["--model", "gemma4:12b"], ["--jobs", "3"], ["--clean", "auto"], ["--inline-math", "auto"], ["--pages-per-batch", "40"]]) {
		assert.equal(a[a.indexOf(pair[0]) + 1], pair[1], pair[0]);
	}
	assert.ok(!a.includes("--no-llm") && !a.includes("--split-chapters") && !a.includes("-o") && !a.includes("--vision-model"));
});

test("buildArgs: toggles, output folder, vision model, pages and extra args", () => {
	const s = { ...DEFAULT_SETTINGS, splitChapters: true, visionModel: " qwen3-vl:8b ", extraArgs: "--no-ocr --timeout 900" };
	const a = buildArgs("/s.py", "/v/a.pdf", s, "/v/out", "10-40");
	assert.ok(a.includes("--split-chapters"));
	assert.equal(a[a.indexOf("-o") + 1], "/v/out");
	assert.equal(a[a.indexOf("--vision-model") + 1], "qwen3-vl:8b");
	assert.equal(a[a.indexOf("--pages") + 1], "10-40");
	assert.deepEqual(a.slice(-3), ["--no-ocr", "--timeout", "900"]);
	const noVision = buildArgs("/s.py", "/v/a.pdf", { ...s, useVision: false });
	assert.ok(noVision.includes("--no-vision") && !noVision.includes("--vision-model"));
	const noLlm = buildArgs("/s.py", "/v/a.pdf", { ...s, useLlm: false });
	assert.ok(noLlm.includes("--no-llm") && !noLlm.includes("--model") && !noLlm.includes("--jobs"));
});

test("clampInt keeps numbers sane", () => {
	assert.equal(clampInt(99, 1, 16), 16);
	assert.equal(clampInt(0, 1, 16), 1);
	assert.equal(clampInt(2.6, 1, 16), 3);
	assert.equal(clampInt(NaN, 1, 16), 1);
	const a = buildArgs("/s.py", "/p.pdf", { ...DEFAULT_SETTINGS, jobs: 500 });
	assert.equal(a[a.indexOf("--jobs") + 1], "16");
});

test("parseLine understands the tool's real output", () => {
	assert.deepEqual(parseLine("  slice 8/14 (pages 281-320): 73 equation(s), 38/43 inline [1620s, ETA ~20 min]"),
		{ kind: "slice", n: 8, total: 14, etaMin: 20 });
	assert.deepEqual(parseLine("  slice 14/14 (pages 521-552): 51 equation(s) [2718s]"), { kind: "slice", n: 14, total: 14, etaMin: undefined });
	assert.deepEqual(parseLine("  cleaned 3/7 chunk(s) submitted"), { kind: "cleaning", done: 3, total: 7 });
	assert.deepEqual(parseLine("[4/4] wrote /v/Books/My Book.md (2719s)"), { kind: "wrote", path: "/v/Books/My Book.md", split: false });
	assert.deepEqual(parseLine("[4/4] wrote 16 notes + index to /v/Books/My Book (2862s)"),
		{ kind: "wrote", path: "/v/Books/My Book", split: true, notes: 16 });
	assert.deepEqual(parseLine("[2/4] gemma4:12b: 7 chunk(s) to clean"), { kind: "stage", stage: 2, text: "gemma4:12b: 7 chunk(s) to clean" });
	assert.equal(parseLine("error: cannot reach Ollama at http://x").kind, "error");
	assert.equal(parseLine("some warning").kind, "other");
	assert.equal(parseLine("").kind, "other");
});

test("statusText", () => {
	assert.equal(statusText({ kind: "slice", n: 2, total: 14, etaMin: 30 }), "PDF→MD: slice 2/14 · ~30 min left");
	assert.equal(statusText({ kind: "slice", n: 14, total: 14 }), "PDF→MD: slice 14/14");
	assert.equal(statusText({ kind: "cleaning", done: 1, total: 2 }), "PDF→MD: cleaning 1/2");
	assert.equal(statusText({ kind: "other" }), null);
});

test("augmentedPath adds the usual uv locations once", () => {
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
