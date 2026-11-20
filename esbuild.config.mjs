import esbuild from "esbuild";
import process from "process";
import { builtinModules } from "node:module";

const production = process.argv[2] === "production";

const context = await esbuild.context({
	entryPoints: ["src/main.ts"],
	bundle: true,
	external: ["obsidian", "electron", "@codemirror/*", "@lezer/*", ...builtinModules, ...builtinModules.map((m) => `node:${m}`)],
	format: "cjs",
	platform: "node",
	target: "es2018",
	logLevel: "info",
	sourcemap: production ? false : "inline",
	treeShaking: true,
	outfile: "main.js",
	// the Python tool and the model-server helper script are embedded as strings and written to the plugin folder at runtime
	loader: { ".py": "text", ".sh": "text" },
});

if (production) {
	await context.rebuild();
	process.exit(0);
} else {
	await context.watch();
}
