// Every flag the plugin can pass must exist in the bundled tool's real --help.
import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { DEFAULT_SETTINGS, buildArgs } from "../.test-build/runner.mjs";

const help = execFileSync(process.env.PYTHON ?? "python3", ["-m", "pdf2md_local", "--help"], { encoding: "utf8", env: { ...process.env, PYTHONPATH: process.cwd() } });

// args = [pdf, ...tool flags]
function flagsIn(args) {
	return args.slice(1).filter((a) => /^--?[a-z]/.test(a));
}

test("flags emitted for the default settings exist in the tool", () => {
	for (const f of flagsIn(buildArgs("a.pdf", DEFAULT_SETTINGS))) assert.ok(help.includes(f), `tool does not know ${f}`);
});

test("flags emitted for every option combination exist in the tool", () => {
	const variants = [
		{ ...DEFAULT_SETTINGS, splitChapters: true, keepPageMarkers: true },
		{ ...DEFAULT_SETTINGS, pageLinks: "on", paddleScale: 4 },
		{ ...DEFAULT_SETTINGS, serveCommand: "serve.sh", pageLinks: "off" },
	];
	for (const s of variants) {
		for (const f of flagsIn(buildArgs("a.pdf", s, "/out", "1-5"))) assert.ok(help.includes(f), `tool does not know ${f}`);
	}
});

test("the tool accepts the option values the settings offer", () => {
	for (const v of ["auto", "on", "off", "obsidian", "plain"]) assert.match(help, new RegExp(v));
});

test("the tool takes several PDFs", () => {
	assert.match(help, /pdf \[pdf \.\.\.\]/);
});

test("setup, check and serve are subcommands that print their own help", () => {
	for (const sub of ["setup", "check", "serve", "selftest", "info"]) {
		const out = execFileSync(process.env.PYTHON ?? "python3", ["-m", "pdf2md_local", sub, "--help"],
			{ encoding: "utf8", env: { ...process.env, PYTHONPATH: process.cwd() } });
		assert.match(out, /usage: pdf2md/);
	}
});
