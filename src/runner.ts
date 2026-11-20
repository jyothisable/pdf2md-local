// Pure helpers: no Obsidian or Node imports, so they can be unit-tested with plain `node --test`.

export type CleanMode = "auto" | "all" | "none";
export type InlineMode = "auto" | "all" | "off";

export interface Settings {
	/** Path or name of the `uv` executable. */
	uvPath: string;
	ollamaHost: string;
	textModel: string;
	/** Empty = use the text model for vision too. */
	visionModel: string;
	/** Parallel Ollama requests. Keep <= OLLAMA_NUM_PARALLEL on the server. */
	jobs: number;
	cleanMode: CleanMode;
	inlineMath: InlineMode;
	/** Write one note per chapter plus an index instead of a single note. */
	splitChapters: boolean;
	pagesPerBatch: number;
	/** Use the vision model for equations, inline maths and figure captions. */
	useVision: boolean;
	/** Use Ollama at all (off = Docling only). */
	useLlm: boolean;
	/** Vault-relative output folder. Empty = next to the PDF. */
	outputFolder: string;
	/** Extra command-line flags, passed through as-is. */
	extraArgs: string;
}

export const DEFAULT_SETTINGS: Settings = {
	uvPath: "uv",
	ollamaHost: "http://localhost:11434",
	textModel: "gemma4:12b",
	visionModel: "",
	jobs: 3,
	cleanMode: "auto",
	inlineMath: "auto",
	splitChapters: false,
	pagesPerBatch: 40,
	useVision: true,
	useLlm: true,
	outputFolder: "",
	extraArgs: "",
};

/** Split a string into arguments, honouring single and double quotes. */
export function splitArgs(text: string): string[] {
	const out: string[] = [];
	let cur = "";
	let quote: string | null = null;
	let started = false;
	for (const ch of text) {
		if (quote) {
			if (ch === quote) quote = null;
			else cur += ch;
		} else if (ch === '"' || ch === "'") {
			quote = ch;
			started = true;
		} else if (/\s/.test(ch)) {
			if (started || cur) out.push(cur);
			cur = "";
			started = false;
		} else {
			cur += ch;
			started = true;
		}
	}
	if (started || cur) out.push(cur);
	return out;
}

/** Arguments for `uv`, running the bundled script on one PDF. */
export function buildArgs(script: string, pdfAbs: string, s: Settings, outAbs?: string, pages?: string): string[] {
	const args = ["run", "--python", "3.12", script, pdfAbs];
	if (outAbs) args.push("-o", outAbs);
	if (!s.useLlm) {
		args.push("--no-llm");
	} else {
		args.push("--model", s.textModel, "--host", s.ollamaHost, "--jobs", String(clampInt(s.jobs, 1, 16)));
		args.push("--clean", s.cleanMode, "--inline-math", s.inlineMath);
		if (!s.useVision) args.push("--no-vision");
		else if (s.visionModel.trim()) args.push("--vision-model", s.visionModel.trim());
	}
	args.push("--pages-per-batch", String(clampInt(s.pagesPerBatch, 0, 500)));
	if (s.splitChapters) args.push("--split-chapters");
	if (pages && pages.trim()) args.push("--pages", pages.trim());
	args.push(...splitArgs(s.extraArgs));
	return args;
}

export function clampInt(v: number, lo: number, hi: number): number {
	const n = Math.round(Number.isFinite(v) ? v : lo);
	return Math.min(hi, Math.max(lo, n));
}

export type Progress =
	| { kind: "slice"; n: number; total: number; etaMin?: number }
	| { kind: "cleaning"; done: number; total: number }
	| { kind: "stage"; stage: number; text: string }
	| { kind: "wrote"; path: string; split: boolean; notes?: number }
	| { kind: "error"; text: string }
	| { kind: "other" };

/** Interpret one line of the tool's output. */
export function parseLine(line: string): Progress {
	const l = line.trim();
	let m = /^slice (\d+)\/(\d+)/.exec(l);
	if (m) {
		const eta = /ETA ~(\d+) min/.exec(l);
		return { kind: "slice", n: +m[1], total: +m[2], etaMin: eta ? +eta[1] : undefined };
	}
	m = /^cleaned (\d+)\/(\d+) chunk/.exec(l);
	if (m) return { kind: "cleaning", done: +m[1], total: +m[2] };
	m = /^\[4\/4\] wrote (\d+) notes \+ index to (.+) \((\d+)s\)$/.exec(l);
	if (m) return { kind: "wrote", path: m[2], split: true, notes: +m[1] };
	m = /^\[4\/4\] wrote (.+) \((\d+)s\)$/.exec(l);
	if (m) return { kind: "wrote", path: m[1], split: false };
	m = /^\[(\d)\/4\] (.*)$/.exec(l);
	if (m) return { kind: "stage", stage: +m[1], text: m[2] };
	if (/^error:/i.test(l)) return { kind: "error", text: l };
	return { kind: "other" };
}

/** Short text for the status bar. */
export function statusText(p: Progress): string | null {
	switch (p.kind) {
		case "slice":
			return `PDF→MD: slice ${p.n}/${p.total}` + (p.etaMin !== undefined ? ` · ~${p.etaMin} min left` : "");
		case "cleaning":
			return `PDF→MD: cleaning ${p.done}/${p.total}`;
		case "stage":
			return `PDF→MD: step ${p.stage}/4`;
		default:
			return null;
	}
}

/** PATH for the child process: GUI-launched apps often lack ~/.local/bin and cargo, where `uv` lives. */
export function augmentedPath(env: Record<string, string | undefined>, home: string, sep = ":"): string {
	const extra = [`${home}/.local/bin`, `${home}/.cargo/bin`, "/usr/local/bin", "/opt/homebrew/bin"];
	const parts = (env.PATH ?? "").split(sep).filter(Boolean);
	for (const e of extra) if (!parts.includes(e)) parts.push(e);
	return parts.join(sep);
}

/** Vault-relative, forward-slash path for an absolute one inside the vault, or null if it is outside. */
export function toVaultPath(base: string, abs: string): string | null {
	const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+$/, "");
	const b = norm(base);
	const a = norm(abs);
	if (a === b) return "";
	return a.startsWith(b + "/") ? a.slice(b.length + 1) : null;
}

/** The note to open after a conversion: the index for a split book, the note itself otherwise. */
export function noteToOpen(w: { path: string; split: boolean }): string {
	return w.split ? `${w.path.replace(/\/+$/, "")}/00 Index.md` : w.path;
}
