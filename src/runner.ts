// Pure helpers: no Obsidian or Node imports, so they can be unit-tested with plain `node --test`.

export type PageLinks = "auto" | "on" | "off";
/** Auto/on/off choice passed to the tool only when it is not auto. */
export type Tri = "auto" | "on" | "off";

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
	/** Theorem-like paragraphs as callouts. Auto = on for Obsidian-style output. */
	callouts: Tri;
	/** Cross-references (Eq. (3.2), Figure 4.1, Theorem 2.1) as links. Auto = on for Obsidian-style output. */
	crossLinks: Tri;
	/** Keep the hidden `<!-- pdf2md:page=N -->` comment that starts every page. */
	keepPageMarkers: boolean;
	/** Ask for confirmation before conversions estimated to take at least this many minutes (0 = never ask). */
	confirmOverMinutes: number;
	/** When a PDF has bookmarks, pick chapters before converting. */
	chooseChapters: boolean;
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
	callouts: "auto",
	crossLinks: "auto",
	keepPageMarkers: false,
	confirmOverMinutes: 5,
	chooseChapters: true,
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
		if (typeof v !== typeof DEFAULT_SETTINGS[key]) continue;
		if ((key === "callouts" || key === "crossLinks" || key === "pageLinks") && v !== "auto" && v !== "on" && v !== "off") continue;
		(out as unknown as Record<string, unknown>)[key] = v;
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
	if (s.callouts !== "auto") args.push("--callouts", s.callouts);
	if (s.crossLinks !== "auto") args.push("--cross-links", s.crossLinks);
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
export function parseCheck(text: string): { ready: boolean; checks: SetupCheck[]; version?: string } | null {
	try {
		const d = JSON.parse(text) as { ready?: unknown; checks?: unknown; version?: unknown } | null;
		if (typeof d?.ready !== "boolean" || !Array.isArray(d.checks)) return null;
		const out: { ready: boolean; checks: SetupCheck[]; version?: string } = { ready: d.ready, checks: d.checks as SetupCheck[] };
		if (typeof d.version === "string" && d.version.trim()) out.version = d.version.trim();
		return out;
	} catch {
		return null;
	}
}

/** The terminal command that installs exactly the tool version that matches this plugin version. */
export function updateCommand(pluginVersion: string): string {
	return `uv tool install --force --reinstall git+https://github.com/jyothisable/pdf2md-local@${pluginVersion}`;
}

const majorMinor = (v: string): string | null => {
	const m = /^v?(\d+)\.(\d+)/.exec(v.trim());
	return m ? `${m[1]}.${m[2]}` : null;
};

/** A warning when the tool's version is unknown or differs (major.minor) from the plugin's; null when they match. */
export function versionWarning(cliVersion: string | null | undefined, pluginVersion: string): string | null {
	if (!cliVersion) return `The installed pdf2md does not report its version, so it is older than this plugin (${pluginVersion}). Some features may not work until you update it.`;
	const a = majorMinor(cliVersion);
	const b = majorMinor(pluginVersion);
	if (a === null || b === null || a === b) return null;
	return `The installed pdf2md is version ${cliVersion} but this plugin is version ${pluginVersion}. Some features may not work until you update it.`;
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
export function setupProblem(c: { ready: boolean; checks: SetupCheck[]; version?: string }, hasServeCommand: boolean): string | null {
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
export function toVaultPath(base: string, abs: string, realBase?: string | null): string | null {
	const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+$/, "");
	const a = norm(abs);
	// The tool prints resolved paths, so a vault reached through a symlink must also match by its real location.
	for (const candidate of [base, realBase]) {
		if (!candidate) continue;
		const b = norm(candidate);
		if (a === b) return "";
		if (a.startsWith(b + "/")) return a.slice(b.length + 1);
	}
	return null;
}

/** The note to open after a conversion: the index for a split book, the note itself otherwise. */
export function noteToOpen(w: { path: string; split: boolean }): string {
	return w.split ? `${w.path.replace(/\/+$/, "")}/00 Index.md` : w.path;
}

/** Short status for a batch: "PDF 2/5", or empty for a single file. */
export function batchLabel(p: Progress | null): string {
	return p && p.kind === "pdf" && p.total > 1 ? `PDF ${p.n}/${p.total}` : "";
}

/** Parse page text ("7", "10-40", or a list like "10-40,60-80,95") into ranges; null if invalid. Empty = no ranges. */
export function parsePageList(text: string): [number, number][] | null {
	const t = text.trim();
	if (!t) return [];
	const out: [number, number][] = [];
	for (const part of t.split(",")) {
		const m = /^\s*(\d+)(?:\s*-\s*(\d+))?\s*$/.exec(part);
		if (!m) return null;
		const a = +m[1];
		const b = m[2] === undefined ? a : +m[2];
		if (a < 1 || b < a) return null;
		out.push([a, b]);
	}
	return out;
}

/** Sort ranges and merge those that overlap or touch. */
export function mergeRanges(ranges: [number, number][]): [number, number][] {
	const sorted = ranges.map((r) => [...r] as [number, number]).sort((x, y) => x[0] - y[0] || x[1] - y[1]);
	const out: [number, number][] = [];
	for (const r of sorted) {
		const last = out[out.length - 1];
		if (last && r[0] <= last[1] + 1) last[1] = Math.max(last[1], r[1]);
		else out.push(r);
	}
	return out;
}

/** Ranges as the tool's page spec: "10-40,60-80,95". */
export function formatRanges(ranges: [number, number][]): string {
	return ranges.map(([a, b]) => (a === b ? String(a) : `${a}-${b}`)).join(",");
}

/** Valid page text: empty (all pages), "7", "10-40", or a comma-separated list such as "10-40,60-80,95". */
export function validPages(text: string): boolean {
	return parsePageList(text) !== null;
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

// ---- output folder ------------------------------------------------------------------------------------------

/** Check the "output folder" setting. Returns the normalised vault-relative folder, or why it is not allowed. */
export function checkOutputFolder(raw: string, configDir: string): { ok: true; folder: string } | { ok: false; reason: string } {
	const t = raw.trim();
	if (!t) return { ok: true, folder: "" };
	if (/^[\\/]/.test(t) || /^[A-Za-z]:/.test(t)) return { ok: false, reason: "the output folder must be a folder inside the vault, not an absolute path." };
	const parts = t.replace(/\\/g, "/").split("/").filter((x) => x !== "" && x !== ".");
	if (parts.includes("..")) return { ok: false, reason: "the output folder must not contain \"..\"." };
	const folder = parts.join("/");
	const cfg = configDir.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").toLowerCase();
	if (cfg && (folder.toLowerCase() === cfg || folder.toLowerCase().startsWith(cfg + "/"))) {
		return { ok: false, reason: "the output folder must not be inside the Obsidian configuration folder." };
	}
	return { ok: true, folder };
}

// ---- RESULT lines, review report, retry ---------------------------------------------------------------------

export interface ConvertResult {
	pdf: string;
	output: string;
	folder: string | null;
	notes: string[];
	pagesFailed: number[];
	formulaImages: { page: number; note: string }[];
	mathWarnings: { note: string; count: number }[];
	cacheKept: boolean;
	/** Absent when the tool is older than 0.7.0. */
	callouts?: number;
	crossLinks?: number;
	/** Links made from the PDF's own internal link annotations (0.7.0+ tools only). */
	pdfLinks?: number;
	/** Web links kept from the PDF. */
	urlLinks?: number;
}

const isInt = (x: unknown): x is number => typeof x === "number" && Number.isInteger(x);
const isStr = (x: unknown): x is string => typeof x === "string" && x !== "";

/** Parse a `RESULT <json>` line; null for anything else or malformed content. */
export function parseResult(line: string): ConvertResult | null {
	const l = line.trim();
	if (!l.startsWith("RESULT ")) return null;
	let d: Record<string, unknown>;
	try {
		const v: unknown = JSON.parse(l.slice(7));
		if (!v || typeof v !== "object" || Array.isArray(v)) return null;
		d = v as Record<string, unknown>;
	} catch {
		return null;
	}
	if (!isStr(d.pdf) || !isStr(d.output)) return null;
	const arr = (x: unknown): unknown[] => (Array.isArray(x) ? x : []);
	const obj = (x: unknown): Record<string, unknown> | null => (x && typeof x === "object" && !Array.isArray(x) ? (x as Record<string, unknown>) : null);
	const formulaImages: ConvertResult["formulaImages"] = [];
	for (const e of arr(d.formula_images)) {
		const o = obj(e);
		if (o && isInt(o.page) && isStr(o.note)) formulaImages.push({ page: o.page, note: o.note });
	}
	const mathWarnings: ConvertResult["mathWarnings"] = [];
	for (const e of arr(d.math_warnings)) {
		const o = obj(e);
		if (o && isStr(o.note) && isInt(o.count) && o.count > 0) mathWarnings.push({ note: o.note, count: o.count });
	}
	const res: ConvertResult = {
		pdf: d.pdf,
		output: d.output,
		folder: isStr(d.folder) ? d.folder : null,
		notes: arr(d.notes).filter(isStr),
		pagesFailed: arr(d.pages_failed).filter(isInt).sort((a, b) => a - b),
		formulaImages,
		mathWarnings,
		cacheKept: d.cache_kept === true,
	};
	if (isInt(d.callouts) && d.callouts >= 0) res.callouts = d.callouts;
	if (isInt(d.cross_links) && d.cross_links >= 0) res.crossLinks = d.cross_links;
	if (isInt(d.pdf_links) && d.pdf_links >= 0) res.pdfLinks = d.pdf_links;
	if (isInt(d.url_links) && d.url_links >= 0) res.urlLinks = d.url_links;
	return res;
}

/** "12 callouts, 48 cross-reference links (30 from the PDF's own links), 5 web links"; null when nothing was reported or all are zero. */
export function enrichmentSummary(r: ConvertResult): string | null {
	const n = (x: number | undefined) => x ?? 0;
	if (n(r.callouts) + n(r.crossLinks) + n(r.pdfLinks) + n(r.urlLinks) <= 0) return null;
	const plural = (k: number, word: string) => `${k} ${word}${k === 1 ? "" : "s"}`;
	const parts: string[] = [];
	if (r.callouts !== undefined) parts.push(plural(r.callouts, "callout"));
	if (r.crossLinks !== undefined) {
		const own = n(r.pdfLinks) > 0 ? ` (${r.pdfLinks} from the PDF's own links)` : "";
		parts.push(`${plural(r.crossLinks, "cross-reference link")}${own}`);
	} else if (n(r.pdfLinks) > 0) {
		parts.push(`${plural(n(r.pdfLinks), "link")} from the PDF's own links`);
	}
	if (r.urlLinks !== undefined && r.urlLinks > 0) parts.push(plural(r.urlLinks, "web link"));
	return parts.join(", ");
}

/** Does this result have anything the user should look at? */
export function needsReview(r: ConvertResult): boolean {
	return r.pagesFailed.length > 0 || r.formulaImages.length > 0 || r.mathWarnings.length > 0;
}

/** The results that need review, in order. Empty means "Nothing needs review". */
export function reviewItems(results: ConvertResult[]): ConvertResult[] {
	return results.filter(needsReview);
}

export function baseName(p: string): string {
	const parts = p.replace(/\\/g, "/").replace(/\/+$/, "").split("/");
	return parts[parts.length - 1] ?? p;
}

/** Parent folder of an absolute path, keeping the path's own separators. */
export function dirName(p: string): string {
	const t = p.replace(/[\\/]+$/, "");
	const i = Math.max(t.lastIndexOf("/"), t.lastIndexOf("\\"));
	if (i < 0) return ".";
	return i === 0 ? t.slice(0, 1) : t.slice(0, i);
}

/**
 * What to trash and which `-o` to use so a re-run lands in the same place: a single note is rewritten at its own
 * path; a split book lives in a folder, whose parent folder is the `-o` value.
 */
export function retryOutput(output: string, folder: string | null): { trash: string; out: string } {
	return folder ? { trash: folder, out: dirName(folder) } : { trash: output, out: output };
}

/** Choose the PDF for a note by name: the best match by shared folders, or the tied choices to ask about. */
export function pickPdf(notePath: string, candidates: string[]): { pick: string | null; choices: string[] } {
	if (candidates.length === 0) return { pick: null, choices: [] };
	if (candidates.length === 1) return { pick: candidates[0], choices: [] };
	const dirs = (p: string) => p.replace(/\\/g, "/").split("/").slice(0, -1);
	const note = dirs(notePath);
	const score = (c: string) => {
		const d = dirs(c);
		let n = 0;
		while (n < d.length && n < note.length && d[n] === note[n]) n++;
		return n;
	};
	const best = Math.max(...candidates.map(score));
	const tied = candidates.filter((c) => score(c) === best);
	return tied.length === 1 ? { pick: tied[0], choices: [] } : { pick: null, choices: tied };
}

/** The page range to reuse when retrying, from a note's `pages` frontmatter (or the job's own value): undefined = all pages. */
export function retryPages(value: unknown): string | undefined {
	const t = typeof value === "number" ? String(value) : typeof value === "string" ? value.trim() : "";
	const ranges = t ? parsePageList(t) : null;
	return ranges && ranges.length ? formatRanges(mergeRanges(ranges)) : undefined;
}

// ---- chapter picker (bookmarks) -----------------------------------------------------------------------------

export interface OutlineEntry {
	title: string;
	level: number;
	first: number;
	last: number;
}

export interface Outline {
	pages: number;
	secPerPage: number;
	measured: boolean;
	entries: OutlineEntry[];
}

/** Arguments for `pdf2md outline`. */
export function buildOutlineArgs(pdfAbs: string): string[] {
	return ["outline", "--json", pdfAbs];
}

/** Parse `pdf2md outline --json`; null if the output is not what the tool prints. */
export function parseOutline(text: string): Outline | null {
	try {
		const d = JSON.parse(text) as Record<string, unknown> | null;
		if (!d || typeof d !== "object" || Array.isArray(d) || !Array.isArray(d.entries)) return null;
		if (!isInt(d.pages) || d.pages < 0) return null;
		const spp = typeof d.sec_per_page === "number" && Number.isFinite(d.sec_per_page) && d.sec_per_page >= 0 ? d.sec_per_page : null;
		if (spp === null) return null;
		const entries: OutlineEntry[] = [];
		for (const e of d.entries) {
			const o = e as Record<string, unknown> | null;
			if (!o || typeof o !== "object" || typeof o.title !== "string" || !isInt(o.level) || !isInt(o.first) || !isInt(o.last)) return null;
			if (o.level < 1 || o.first < 1 || o.last < o.first) return null;
			entries.push({ title: o.title, level: o.level, first: o.first, last: o.last });
		}
		return { pages: d.pages, secPerPage: spp, measured: d.measured === true, entries };
	} catch {
		return null;
	}
}

/** Indexes of all entries below entry `i` (the following entries that are deeper), in document order. */
export function descendants(entries: OutlineEntry[], i: number): number[] {
	const out: number[] = [];
	for (let j = i + 1; j < entries.length && entries[j].level > entries[i].level; j++) out.push(j);
	return out;
}

/** Indexes of the direct children of entry `i`. */
export function childrenOf(entries: OutlineEntry[], i: number): number[] {
	return descendants(entries, i).filter((j) => entries[j].level === entries[i].level + 1);
}

/** Index of the parent entry, or -1 for a top-level one. */
export function parentOf(entries: OutlineEntry[], i: number): number {
	for (let j = i - 1; j >= 0; j--) if (entries[j].level < entries[i].level) return j;
	return -1;
}

/**
 * Check or uncheck entry `i`: its descendants follow, and each ancestor is checked exactly when all of its
 * descendants are. Returns a new set.
 */
export function toggleEntry(entries: OutlineEntry[], selected: ReadonlySet<number>, i: number, checked: boolean): Set<number> {
	const out = new Set(selected);
	for (const j of [i, ...descendants(entries, i)]) {
		if (checked) out.add(j);
		else out.delete(j);
	}
	for (let p = parentOf(entries, i); p >= 0; p = parentOf(entries, p)) {
		if (descendants(entries, p).every((j) => out.has(j))) out.add(p);
		else out.delete(p);
	}
	return out;
}

/** Display state of a row: a parent with only some of its descendants selected is "mixed" (indeterminate). */
export function checkState(entries: OutlineEntry[], selected: ReadonlySet<number>, i: number): "checked" | "unchecked" | "mixed" {
	if (selected.has(i)) return "checked";
	return descendants(entries, i).some((j) => selected.has(j)) ? "mixed" : "unchecked";
}

/** Merged page ranges covered by the selected entries. */
export function selectionRanges(entries: OutlineEntry[], selected: Iterable<number>): [number, number][] {
	const r: [number, number][] = [];
	for (const i of selected) if (entries[i]) r.push([entries[i].first, entries[i].last]);
	return mergeRanges(r);
}

/** The `--pages` value for the selected entries, merged like the tool does ("10-40,60-80,95"); "" if none. */
export function selectionToPages(entries: OutlineEntry[], selected: Iterable<number>): string {
	return formatRanges(selectionRanges(entries, selected));
}

export function countPages(ranges: [number, number][]): number {
	return ranges.reduce((n, [a, b]) => n + (b - a + 1), 0);
}

/** Estimated seconds for a number of pages. */
export function estimateSeconds(pages: number, secPerPage: number): number {
	return Math.max(0, pages) * Math.max(0, secPerPage);
}

/** "pp. 13–28 · about 2 min" for one row. */
export function rowLabel(e: OutlineEntry, secPerPage: number): string {
	const n = e.last - e.first + 1;
	const range = n === 1 ? `p. ${e.first}` : `pp. ${e.first}–${e.last}`;
	return `${range} · ${formatDuration(estimateSeconds(n, secPerPage))}`;
}

/** "120 pages · about 6 min", or "Nothing selected". */
export function totalLabel(ranges: [number, number][], secPerPage: number): string {
	const n = countPages(ranges);
	if (!n) return "Nothing selected";
	return `${n} page${n === 1 ? "" : "s"} · about ${formatDuration(estimateSeconds(n, secPerPage))}`;
}

/** Offer the chapter picker first? Needs the setting on and at least two bookmarks (one entry is the whole book). */
export function shouldOfferChapters(enabled: boolean, outline: Outline | null): boolean {
	return enabled && outline !== null && outline.entries.length >= 2;
}

/**
 * The `--pages` value for a picker choice: undefined when every entry is selected (convert the whole PDF with no
 * `--pages`, so checkpoints and frontmatter stay as for a plain conversion), "" when nothing is selected.
 */
export function chapterChoicePages(entries: OutlineEntry[], selected: ReadonlySet<number>): string | undefined {
	if (entries.length > 0 && entries.every((_, i) => selected.has(i))) return undefined;
	return selectionToPages(entries, selected);
}
