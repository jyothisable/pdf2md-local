// Every flag the plugin can pass must exist in the bundled tool's real --help.
import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { DEFAULT_SETTINGS, buildArgs } from "../.test-build/runner.mjs";

const help = execFileSync(process.env.PYTHON ?? "python3", ["tool/pdf_to_md.py", "--help"], { encoding: "utf8" });

// args = ["run", "--python", "3.12", script, pdf, ...tool flags]; only the part after the PDF is the tool's
function flagsIn(args) {
	return args.slice(5).filter((a) => /^--?[a-z]/.test(a));
}

test("flags emitted for the default settings exist in the tool", () => {
	for (const f of flagsIn(buildArgs("s.py", "a.pdf", DEFAULT_SETTINGS))) {
		assert.ok(help.includes(f), `tool does not know ${f}`);
	}
});

test("flags emitted for every option combination exist in the tool", () => {
	const variants = [
		{ ...DEFAULT_SETTINGS, splitChapters: true, visionModel: "m", useVision: true },
		{ ...DEFAULT_SETTINGS, useVision: false },
		{ ...DEFAULT_SETTINGS, useLlm: false },
		{ ...DEFAULT_SETTINGS, cleanMode: "all", inlineMath: "off" },
		{ ...DEFAULT_SETTINGS, cleanMode: "none", inlineMath: "all", pagesPerBatch: 0 },
		{ ...DEFAULT_SETTINGS, api: "openai", visionApi: "openai", visionHost: "http://h", pageLinks: "on", equationFlags: false },
		{ ...DEFAULT_SETTINGS, engine: "paddleocr-vl", serveCommand: "serve.sh", splitChapters: true },
		{ ...DEFAULT_SETTINGS, pageLinks: "off" },
	];
	for (const s of variants) {
		for (const f of flagsIn(buildArgs("s.py", "a.pdf", s, "/out", "1-5"))) {
			assert.ok(help.includes(f), `tool does not know ${f}`);
		}
	}
});

test("the tool accepts the option values the settings offer", () => {
	for (const v of ["auto", "all", "none"]) assert.match(help, new RegExp(v));
	for (const v of ["auto", "all", "off"]) assert.match(help, new RegExp(v));
	for (const v of ["ollama", "openai", "docling", "paddleocr-vl", "on"]) assert.match(help, new RegExp(v));
});

test("the tool takes several PDFs", () => {
	assert.match(help, /pdf \[pdf \.\.\.\]/);
});
