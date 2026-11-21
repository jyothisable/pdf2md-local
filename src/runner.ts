// Pure helpers: no Obsidian or Node imports, so they can be unit-tested with plain `node --test`.

export type PageLinks = "auto" | "on" | "off";

export interface Settings {
	/** Name or full path of the `pdf2md` command (installed with `uv tool install`; see the README). */
	pdf2mdPath: string;
	/** The llama-server running PaddleOCR-VL. */
	paddleServer: string;
	/** Raster scale: 2.5 = 180 dpi (balanced); 4 = 288 dpi (table-heavy PDFs). */
	paddleScale: number;
	/** Command that starts the server if nothing answers; empty = `pdf2md serve`. The tool stops it again when done. */
	serveCommand: string;
	pageLinks: PageLinks;
	/** Keep the hidden `<!-- pdf2md:page=N -->` comment that starts every page. */
	keepPageMarkers: boolean;
	/** Ask for confirmation before conversions estimated to take at least this many minutes (0 = never ask). */
	confirmOverMinutes: number;
	/** Ask which pages to convert before every single-PDF conversion. */
	askForPages: boolean;
	/** Open the progress window when a conversion starts (it can be closed to run in the background). */
	progressWindow: boolean;
	/** Write one note per chapter plus an index instead of a single note. */
	splitChapters: boolean;
	/** Vault-relative output folder. Empty = next to the PDF. */
	outputFolder: string;
	/** Extra command-line flags, passed through as-is. */
	extraArgs: string;
}

export const DEFAULT_SETTINGS: Settings = {
	pdf2mdPath: "pdf2md",
	paddleServer: "http://127.0.0.1:8111/v1",
	paddleScale: 2.5,
	serveCommand: "",
	pageLinks: "auto",
	keepPageMarkers: false,
	confirmOverMinutes: 5,
	askForPages: false,
	progressWindow: true,
	splitChapters: false,
	outputFolder: "",
	extraArgs: "",
};

/** Keep only the settings this version knows, so data saved by older versions cannot inject stale options. */
export function migrateSettings(saved: Record<string, unknown> | null | undefined): Settings {
	const out: Settings = { ...DEFAULT_SETTINGS };
	for (const key of Object.keys(DEFAULT_SETTINGS) as (keyof Settings)[]) {
		const v = saved?.[key];
		if (typeof v === typeof DEFAULT_SETTINGS[key]) (out as unknown as Record<string, unknown>)[key] = v;
	}
	return out;
}

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

/** Quote one argument for the tool's own command-line splitting (it uses shell-style rules). */
export function quoteArg(a: string): string {
	return /^[\w@%+=:,./-]+$/.test(a) ? a : `"${a.replace(/(["\\])/g, "\\$1")}"`;
}

/** Arguments for `pdf2md`, converting the given PDFs. */
export function buildArgs(pdfAbs: string | string[], s: Settings, outAbs?: string, pages?: string): string[] {
	const pdfs = Array.isArray(pdfAbs) ? pdfAbs : [pdfAbs];
	const args = [...pdfs];
	if (outAbs) args.push("-o", outAbs);
	args.push("--flavor", "obsidian", "--paddle-server", s.paddleServer.trim() || DEFAULT_SETTINGS.paddleServer);
	const scale = Number.isFinite(s.paddleScale) ? Math.min(4, Math.max(1, s.paddleScale)) : 2.5;
	args.push("--paddle-scale", String(scale));
	args.push("--serve-command", s.serveCommand.trim() || `${quoteArg(s.pdf2mdPath.trim() || "pdf2md")} serve`);
	args.push("--page-links", s.pageLinks);
	if (s.keepPageMarkers) args.push("--page-markers");
	if (s.splitChapters) args.push("--split-chapters");
	if (pages && pages.trim()) args.push("--pages", pages.trim());
	args.push(...splitArgs(s.extraArgs));
	return args;
}

export interface SetupCheck {
	id: string;
	ok: boolean;
	required: boolean;
	detail: string;
	fix: string;
}

/** Parse `pdf2md check --json`; null if the output is not what the tool prints. */
export function parseCheck(text: string): { ready: boolean; checks: SetupCheck[] } | null {
	try {
		const d = JSON.parse(text) as { ready?: unknown; checks?: unknown } | null;
		if (typeof d?.ready !== "boolean" || !Array.isArray(d.checks)) return null;
		return { ready: d.ready, checks: d.checks as SetupCheck[] };
	} catch {
		return null;
	}
}

export interface Estimate {
	files: { name: string; pages: number | null; seconds: number | null }[];
	pages: number;
	seconds: number;
	unknown: number;
	sec_per_page: number;
	measured: boolean;
}

/** Parse `pdf2md info --json`; null if the output is not what the tool prints. */
export function parseEstimate(text: string): Estimate | null {
	try {
		const d = JSON.parse(text) as Partial<Estimate> | null;
		if (!d || !Array.isArray(d.files) || typeof d.pages !== "number" || typeof d.seconds !== "number") return null;
		return d as Estimate;
	} catch {
		return null;
	}
}

/** Arguments for `pdf2md info`: PDFs, and the page range when a single PDF is converted. */
export function buildInfoArgs(pdfAbs: string[], pages?: string): string[] {
	const args = ["info", "--json", ...pdfAbs];
	if (pages && pages.trim() && pdfAbs.length === 1) args.push("--pages", pages.trim());
	return args;
}

/** One line for a notice or the confirmation window. */
export function estimateText(e: Estimate, serverRunning: boolean): string {
	const unit = e.pages === 1 ? "page" : "pages";
	const time = e.pages > 0 ? `about ${formatDuration(e.seconds)}` : "time unknown";
	const speed = e.measured ? "from your recent conversions" : "a typical GPU speed";
	const more = e.unknown ? `, plus ${e.unknown} PDF${e.unknown === 1 ? "" : "s"} of unknown length` : "";
	const server = serverRunning ? "The model server is already running." : "The model server will be started first.";
	return `${e.pages} ${unit}${more}: ${time} (${speed}). ${server}`;
}

/** Should the user be asked before starting? */
export function needsConfirmation(e: Estimate, thresholdMinutes: number): boolean {
	return thresholdMinutes > 0 && e.seconds >= thresholdMinutes * 60;
}

/** What to tell the user when the setup is incomplete, or null if a conversion can start. */
export function setupProblem(c: { ready: boolean; checks: SetupCheck[] }, hasServeCommand: boolean): string | null {
	const missing = c.checks.filter((x) => x.required && !x.ok);
	if (missing.length) return `${missing[0].detail} is missing. In a terminal run: ${missing[0].fix}`;
	const byId = (id: string) => c.checks.find((x) => x.id === id);
	const server = byId("server");
	const llama = byId("llama-server");
	if (server && !server.ok && !hasServeCommand && llama && !llama.ok) {
		return `no model server is running and llama-server was not found. ${llama.fix}`;
	}
	return null;
}

export function clampInt(v: number, lo: number, hi: number): number {
	const n = Math.round(Number.isFinite(v) ? v : lo);
	return Math.min(hi, Math.max(lo, n));
}

export type Progress =
	| { kind: "pages"; done: number; total: number }
	| { kind: "pdf"; n: number; total: number; name: string }
	| { kind: "slice"; n: number; total: number; etaMin?: number }
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
		case "stage":
			return `PDF→MD: ${pre}step ${p.stage}/4`;
		default:
			return null;
	}
}

/** PATH for the child process: GUI-launched apps often lack ~/.local/bin and cargo, where `pdf2md` (uv tool install) lives. */
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

/** Environment for the tool: PATH extended so GUI-launched Obsidian finds pdf2md and llama-server. */
export function toolEnv(env: Record<string, string | undefined>, home: string, sep = ":"): Record<string, string> {
	const out: Record<string, string> = {};
	for (const [k, v] of Object.entries(env)) if (v !== undefined) out[k] = v;
	out.PATH = augmentedPath(env, home, sep);
	out.PYTHONUNBUFFERED = "1";
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
		case "stage":
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
