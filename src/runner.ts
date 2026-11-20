// Pure helpers: no Obsidian or Node imports, so they can be unit-tested with plain `node --test`.

export type CleanMode = "auto" | "all" | "none";
export type InlineMode = "auto" | "all" | "off";
export type Api = "ollama" | "openai";
export type Engine = "docling" | "paddleocr-vl";
export type PageLinks = "auto" | "on" | "off";

export interface Settings {
	/** Path or name of the `uv` executable. */
	uvPath: string;
	/** ollama, or openai = any OpenAI-compatible server (llama.cpp's llama-server, LM Studio, vLLM ...). */
	api: Api;
	/** Address of the model server. For openai a bare host:port gets /v1 added by the tool. */
	ollamaHost: string;
	/** Passed to the tool through the environment (never on the command line). */
	apiKey: string;
	/** Empty = same API as the text model. */
	visionApi: "" | Api;
	/** Empty = same address as the text model. */
	visionHost: string;
	/** docling = Docling layout + a vision model repairing equations; paddleocr-vl = PaddleOCR-VL-1.6 via llama.cpp. */
	engine: Engine;
	/** The llama-server running PaddleOCR-VL (engine paddleocr-vl). */
	paddleServer: string;
	/** Command that starts that server if nothing answers; the tool stops it again when done. */
	serveCommand: string;
	pageLinks: PageLinks;
	/** Add "check this equation" callouts for equations that look cut off. */
	equationFlags: boolean;
	/** Ask which pages to convert before every single-PDF conversion. */
	askForPages: boolean;
	/** Open the progress window when a conversion starts (it can be closed to run in the background). */
	progressWindow: boolean;
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
	api: "ollama",
	ollamaHost: "http://localhost:11434",
	apiKey: "",
	visionApi: "",
	visionHost: "",
	engine: "docling",
	paddleServer: "http://127.0.0.1:8111/v1",
	serveCommand: "",
	pageLinks: "auto",
	equationFlags: true,
	askForPages: false,
	progressWindow: true,
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
export function buildArgs(script: string, pdfAbs: string | string[], s: Settings, outAbs?: string, pages?: string): string[] {
	const pdfs = Array.isArray(pdfAbs) ? pdfAbs : [pdfAbs];
	const args = ["run", "--python", "3.12", script, ...pdfs];
	if (outAbs) args.push("-o", outAbs);
	if (s.engine === "paddleocr-vl") {
		// PaddleOCR-VL reads the pages itself: no model flags, and the tool picks its own (larger) slice size
		args.push("--engine", "paddleocr-vl", "--paddle-server", s.paddleServer.trim() || DEFAULT_SETTINGS.paddleServer);
		if (s.serveCommand.trim()) args.push("--serve-command", s.serveCommand.trim());
	} else {
		if (!s.useLlm) {
			args.push("--no-llm");
		} else {
			args.push("--model", s.textModel, "--host", s.ollamaHost, "--jobs", String(clampInt(s.jobs, 1, 16)));
			args.push("--api", s.api, "--inline-math", s.inlineMath, "--clean", s.cleanMode);
			if (!s.useVision) {
				args.push("--no-vision");
			} else {
				if (s.visionModel.trim()) args.push("--vision-model", s.visionModel.trim());
				if (s.visionApi) args.push("--vision-api", s.visionApi);
				if (s.visionHost.trim()) args.push("--vision-host", s.visionHost.trim());
			}
		}
		args.push("--pages-per-batch", String(clampInt(s.pagesPerBatch, 0, 500)));
		if (!s.equationFlags) args.push("--no-equation-flags");
	}
	args.push("--page-links", s.pageLinks);
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
	| { kind: "pages"; done: number; total: number }
	| { kind: "pdf"; n: number; total: number; name: string }
	| { kind: "slice"; n: number; total: number; etaMin?: number }
	| { kind: "cleaning"; done: number; total: number }
	| { kind: "stage"; stage: number; text: string }
	| { kind: "wrote"; path: string; split: boolean; notes?: number }
	| { kind: "error"; text: string }
	| { kind: "other" };

/** Interpret one line of the tool's output. */
export function parseLine(line: string): Progress {
	const l = line.trim();
	let m = /^PROGRESS (\d+) (\d+)$/.exec(l);
	if (m) return { kind: "pages", done: +m[1], total: +m[2] };
	m = /^=== PDF (\d+)\/(\d+): (.+)$/.exec(l);
	if (m) return { kind: "pdf", n: +m[1], total: +m[2], name: m[3] };
	m = /^slice (\d+)\/(\d+)/.exec(l);
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

/** Short text for the status bar. `batch` is the current "PDF 2/5" prefix, if several PDFs are running. */
export function statusText(p: Progress, batch = ""): string | null {
	const pre = batch ? `${batch} · ` : "";
	switch (p.kind) {
		case "pdf":
			return `PDF→MD: PDF ${p.n}/${p.total}`;
		case "slice":
			return `PDF→MD: ${pre}slice ${p.n}/${p.total}` + (p.etaMin !== undefined ? ` · ~${p.etaMin} min left` : "");
		case "cleaning":
			return `PDF→MD: ${pre}cleaning ${p.done}/${p.total}`;
		case "stage":
			return `PDF→MD: ${pre}step ${p.stage}/4`;
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

/** Base URL of an OpenAI-compatible server, as the tool computes it: a bare host:port gets /v1. */
export function apiBase(host: string): string {
	const h = host.trim().replace(/\/+$/, "");
	let path = "";
	try {
		path = new URL(h).pathname;
	} catch {
		return h;
	}
	return path === "" || path === "/" ? `${h}/v1` : h;
}

/** Short status for a batch: "PDF 2/5", or empty for a single file. */
export function batchLabel(p: Progress | null): string {
	return p && p.kind === "pdf" && p.total > 1 ? `PDF ${p.n}/${p.total}` : "";
}

/** Valid page-range text: empty (all pages), "7", or "10-40". */
export function validPages(text: string): boolean {
	const t = text.trim();
	if (!t) return true;
	const m = /^(\d+)(?:\s*-\s*(\d+))?$/.exec(t);
	return !!m && +m[1] >= 1 && (m[2] === undefined || +m[2] >= +m[1]);
}

/** Environment for the tool: PATH extended for uv, and the API key (never put on the command line). */
export function toolEnv(env: Record<string, string | undefined>, home: string, apiKey: string, sep = ":"): Record<string, string> {
	const out: Record<string, string> = {};
	for (const [k, v] of Object.entries(env)) if (v !== undefined) out[k] = v;
	out.PATH = augmentedPath(env, home, sep);
	out.PYTHONUNBUFFERED = "1";
	if (apiKey.trim()) out.PDF2MD_API_KEY = apiKey.trim();
	return out;
}

// ---- progress window ---------------------------------------------------------------------------------------

export interface Sample {
	t: number;
	done: number;
}

export interface ProgressState {
	startedAt: number;
	pdfIndex: number;
	pdfTotal: number;
	pdfName: string;
	pdfStartedAt: number;
	pagesDone: number;
	pagesTotal: number;
	samples: Sample[];
	stage: string;
	finishedMs: number[];
	wrote: number;
	lastError: string;
}

export function initialState(now: number, pdfTotal: number): ProgressState {
	return {
		startedAt: now, pdfIndex: 1, pdfTotal, pdfName: "", pdfStartedAt: now, pagesDone: 0, pagesTotal: 0,
		samples: [], stage: "Starting", finishedMs: [], wrote: 0, lastError: "",
	};
}

/** Fold one line of tool output into the progress state. Pure: the window and the status bar both read this. */
export function reduce(s: ProgressState, p: Progress, now: number): ProgressState {
	switch (p.kind) {
		case "pdf":
			return { ...s, pdfIndex: p.n, pdfTotal: p.total, pdfName: p.name, pdfStartedAt: now, pagesDone: 0,
				pagesTotal: 0, samples: [], stage: "Starting" };
		case "pages": {
			const samples = [...s.samples, { t: now, done: p.done }].slice(-300);
			const reading = p.total <= 0 || p.done < p.total;
			return { ...s, pagesDone: p.done, pagesTotal: p.total, samples, stage: reading ? "Reading pages" : "Finishing up" };
		}
		case "cleaning":
			return { ...s, stage: `Cleaning text (${p.done}/${p.total})` };
		case "stage":
			if (p.stage === 2) return { ...s, stage: "Cleaning text" };
			if (p.stage === 3) return { ...s, stage: "Checking the maths" };
			return s;
		case "wrote":
			return { ...s, stage: "Saved", wrote: s.wrote + 1, finishedMs: [...s.finishedMs, now - s.pdfStartedAt] };
		case "error":
			return { ...s, lastError: p.text };
		default:
			return s;
	}
}

/** Share of the whole job done, 0..1 (several PDFs count equally). */
export function fractionOverall(s: ProgressState): number {
	const cur = s.pagesTotal > 0 ? Math.min(1, s.pagesDone / s.pagesTotal) : 0;
	return Math.min(1, (s.pdfIndex - 1 + cur) / Math.max(1, s.pdfTotal));
}

/**
 * Seconds left, or null while there is not enough data. The speed is measured over the last five minutes, so pages
 * that were skipped instantly because a resumed run already had them do not make the estimate too optimistic.
 */
export function etaSeconds(s: ProgressState, now: number): number | null {
	if (s.pagesTotal <= 0) return null;
	const recent = s.samples.filter((x) => now - x.t <= 300_000);
	const pool = recent.length >= 2 ? recent : s.samples.slice(-2);   // slow progress: just the last two readings
	if (pool.length < 2) return null;
	const first = pool[0];
	const last = pool[pool.length - 1];
	const rate = (last.done - first.done) / Math.max(1, (last.t - first.t) / 1000);
	if (!(rate > 0)) return null;
	const cur = Math.max(0, s.pagesTotal - s.pagesDone) / rate;
	const waiting = Math.max(0, s.pdfTotal - s.pdfIndex);
	if (!waiting) return cur;
	const perPdf = s.finishedMs.length
		? s.finishedMs.reduce((a, b) => a + b, 0) / s.finishedMs.length / 1000
		: (now - s.pdfStartedAt) / 1000 + cur;
	return cur + waiting * perPdf;
}

export function formatDuration(sec: number): string {
	if (sec < 60) return `${Math.max(1, Math.round(sec))} s`;
	if (sec < 5400) return `${Math.max(1, Math.round(sec / 60))} min`;
	const h = Math.floor(sec / 3600);
	const m = Math.round((sec - h * 3600) / 60);
	return m === 60 ? `${h + 1} h` : `${h} h ${m} min`;
}

export function etaLabel(s: ProgressState, now: number): string {
	const e = etaSeconds(s, now);
	if (e === null) return "estimating…";
	return e < 5 ? "almost done" : `about ${formatDuration(e)} left`;
}

/** Compact text for the clickable status bar item. */
export function statusBarText(s: ProgressState, now: number): string {
	const pct = Math.round(fractionOverall(s) * 100);
	const e = etaSeconds(s, now);
	const batch = s.pdfTotal > 1 ? `PDF ${s.pdfIndex}/${s.pdfTotal} · ` : "";
	return `PDF→MD: ${batch}${pct}%` + (e !== null && e >= 5 ? ` · ~${formatDuration(e)}` : "");
}
