import {
	App,
	FileSystemAdapter,
	FuzzySuggestModal,
	Modal,
	Notice,
	Plugin,
	PluginSettingTab,
	Setting,
	TAbstractFile,
	TFile,
	TFolder,
	normalizePath,
	requireApiVersion,
} from "obsidian";
import { ChildProcess, execFile, spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import {
	ConvertResult,
	DEFAULT_SETTINGS,
	Outline,
	SetupCheck,
	Tri,
	buildInfoArgs,
	buildOutlineArgs,
	checkState,
	chapterChoicePages,
	shouldOfferChapters,
	enrichmentSummary,
	parseOutline,
	rowLabel,
	selectionRanges,
	toggleEntry,
	totalLabel,
	estimateText,
	needsConfirmation,
	parseEstimate,
	PageLinks,
	ProgressState,
	Settings,
	etaLabel,
	formatDuration,
	fractionOverall,
	initialState,
	buildArgs,
	migrateSettings,
	noteToOpen,
	parseCheck,
	parseLine,
	reduce,
	setupProblem,
	statusBarText,
	toVaultPath,
	toolEnv,
	validPages,
	baseName,
	checkOutputFolder,
	parseResult,
	pickPdf,
	retryOutput,
	retryPages,
	reviewItems,
	updateCommand,
	versionWarning,
} from "./runner";

const MAX_LOG_LINES = 3000;
const KILL_AFTER_MS = 10_000;
const SETUP_CACHE_MS = 120_000;

const isPdf = (f: TAbstractFile): f is TFile => f instanceof TFile && f.extension.toLowerCase() === "pdf";

interface Job {
	proc: ChildProcess;
	files: TFile[];
	wrote: { path: string; split: boolean }[];
	results: ConvertResult[];
	errors: string[];
	cancelled: boolean;
	exited: boolean;
	killTimer: number | null;
}

/** What the progress window shows. It outlives the process so the result stays readable after it ends. */
interface RunView {
	running: boolean;
	files: TFile[];
	state: ProgressState;
	wrote: { path: string; split: boolean }[];
	results: ConvertResult[];
	/** The page range this job was started with (single PDF only); a retry must reuse it. */
	pages?: string;
	error?: string;
	endedAt?: number;
}

/** Extra options for a run that is not a plain conversion (retrying failed pages). */
interface RunOptions {
	/** `-o` value to use instead of the output-folder setting. */
	outAbs?: string;
	/** Runs after the setup check, just before the tool starts (e.g. trash the previous output). */
	beforeRun?: () => Promise<void>;
	/** Skip the time estimate (it would describe the whole PDF, not the pages being retried). */
	skipEstimate?: boolean;
}

export default class Pdf2MdPlugin extends Plugin {
	settings: Settings = { ...DEFAULT_SETTINGS };
	private job: Job | null = null;
	/** True from the moment a conversion is requested until it is running or has been abandoned. */
	private starting = false;
	private log: string[] = [];
	private statusEl: HTMLElement | null = null;
	private view: RunView | null = null;
	private modal: ProgressModal | null = null;
	/** A fresh successful setup check, used once by the next conversion start. */
	private setupCache: { value: { ready: boolean; serverUp: boolean }; at: number } | null = null;

	async onload() {
		await this.loadSettings();
		this.statusEl = this.addStatusBarItem();
		this.statusEl.addClass("pdf2md-status");
		this.statusEl.setAttribute("aria-label", "Show the PDF conversion progress");
		this.statusEl.addEventListener("click", () => this.showProgress());
		this.statusEl.hide();
		this.registerInterval(window.setInterval(() => this.tick(), 1000));

		this.addCommand({
			id: "convert-pdf",
			name: "Convert PDF to Markdown",
			callback: () => {
				const active = this.app.workspace.getActiveFile();
				if (active && isPdf(active)) void this.convertOne(active);
				else new PdfPicker(this.app, (f) => void this.convertOne(f)).open();
			},
		});
		this.addCommand({
			id: "convert-pdf-pages",
			name: "Convert PDF pages…",
			callback: () => {
				const active = this.app.workspace.getActiveFile();
				if (active && isPdf(active)) this.askPages(active);
				else new PdfPicker(this.app, (f) => this.askPages(f)).open();
			},
		});
		this.addCommand({
			id: "convert-chapters",
			name: "Convert chapters…",
			checkCallback: (checking) => {
				const active = this.app.workspace.getActiveFile();
				if (!active || !isPdf(active)) return false;
				if (!checking) void this.chooseChapters(active);
				return true;
			},
		});
		this.addCommand({
			id: "convert-several",
			name: "Convert several PDFs…",
			callback: () => {
				const pdfs = this.app.vault.getFiles().filter(isPdf);
				if (!pdfs.length) new Notice("PDF to Markdown: this vault has no PDFs.");
				else new MultiPdfModal(this.app, pdfs, (files) => void this.convertFiles(files)).open();
			},
		});
		this.addCommand({
			id: "retry-failed-pages",
			name: "Retry failed pages",
			checkCallback: (checking) => {
				const note = this.retryableNote();
				if (!note) return false;
				if (!checking) void this.retryFromNote(note);
				return true;
			},
		});
		this.addCommand({
			id: "cancel-conversion",
			name: "Cancel the running PDF conversion",
			callback: () => this.cancel(),
		});
		this.addCommand({
			id: "show-progress",
			name: "Show conversion progress",
			callback: () => this.showProgress(),
		});
		this.addCommand({
			id: "check-setup",
			name: "Check the pdf2md setup",
			callback: () => void this.checkSetup(true),
		});
		this.addCommand({
			id: "show-log",
			name: "Show the last PDF conversion log",
			callback: () => new LogModal(this.app, this.log.join("\n") || "No conversion has run yet.").open(),
		});

		this.registerEvent(
			this.app.workspace.on("file-menu", (menu, file) => {
				if (isPdf(file)) {
					menu.addItem((item) =>
						item
							.setTitle("Convert to Markdown")
							.setIcon("file-text")
							.onClick(() => void this.convertOne(file)),
					);
					menu.addItem((item) =>
						item
							.setTitle("Convert chapters…")
							.setIcon("list-checks")
							.onClick(() => void this.chooseChapters(file)),
					);
				} else if (file instanceof TFolder) {
					const pdfs = collectPdfs(file);
					if (pdfs.length) {
						menu.addItem((item) =>
							item
								.setTitle(`Convert all ${pdfs.length} PDF${pdfs.length === 1 ? "" : "s"} in this folder to Markdown`)
								.setIcon("files")
								.onClick(() => void this.convertFiles(pdfs)),
						);
					}
				}
			}),
		);
		this.registerEvent(
			this.app.workspace.on("files-menu", (menu, files) => {
				const pdfs = files.filter(isPdf);
				if (pdfs.length >= 2) {
					menu.addItem((item) =>
						item
							.setTitle(`Convert ${pdfs.length} PDFs to Markdown`)
							.setIcon("files")
							.onClick(() => void this.convertFiles(pdfs)),
					);
				}
			}),
		);
		this.registerEvent(this.app.workspace.on("quit", () => this.cancel(true)));

		this.addSettingTab(new Pdf2MdSettingTab(this.app, this));
	}

	onunload() {
		this.cancel(true);
	}

	async loadSettings() {
		this.settings = migrateSettings((await this.loadData()) as Record<string, unknown> | null);
	}

	async saveSettings() {
		await this.saveData(this.settings);
	}

	private basePath(): string | null {
		const a = this.app.vault.adapter;
		return a instanceof FileSystemAdapter ? a.getBasePath() : null;
	}

	/** Vault-relative path for an absolute one printed by the tool (it prints resolved paths, so try the real vault path too). */
	toVaultRel(abs: string): string | null {
		const base = this.basePath();
		if (!base) return null;
		try {
			return toVaultPath(base, abs, fs.realpathSync(base));
		} catch {
			return toVaultPath(base, abs);
		}
	}

	// ---- checks before running ---------------------------------------------------------------

	private env() {
		return toolEnv(process.env, os.homedir(), path.delimiter);
	}

	/** Run a `pdf2md` subcommand and return its standard output, or null if the program could not be run. */
	private runTool(args: string[], timeout = 60000): Promise<string | null> {
		return new Promise((resolve) =>
			execFile(this.settings.pdf2mdPath, args, { env: this.env(), timeout }, (err, stdout) =>
				resolve(err && !stdout ? null : String(stdout))),
		);
	}

	/**
	 * Ask the installed `pdf2md` whether it is ready. The plugin never installs or downloads anything: when something
	 * is missing it only shows which terminal command to run, with a copy button.
	 */
	async checkSetup(verbose = false): Promise<{ ready: boolean; serverUp: boolean } | null> {
		const s = this.settings;
		const out = await this.runTool(["check", "--json", "--server", s.paddleServer]);
		const check = out === null ? null : parseCheck(out);
		if (!check) {
			new SetupModal(this.app, s.pdf2mdPath, null, "Could not run the pdf2md command.", null, this.manifest.version).open();
			return null;
		}
		const warning = versionWarning(check.version, this.manifest.version);
		if (warning) new Notice(`PDF to Markdown: ${warning}`, 12000);
		const serverUp = check.checks.find((c) => c.id === "server")?.ok ?? false;
		const problem = setupProblem(check, s.serveCommand.trim() !== "");
		if (problem) {
			new SetupModal(this.app, s.pdf2mdPath, check.checks, problem, warning, this.manifest.version).open();
			return null;
		}
		if (verbose || warning) new SetupModal(this.app, s.pdf2mdPath, check.checks, null, warning, this.manifest.version).open();
		return { ready: true, serverUp };
	}

	/** Pages and an estimated time; asks first when the job is long. Returns false if the user backs out. */
	private async confirmEstimate(files: TFile[], base: string, pages: string | undefined, serverUp: boolean): Promise<boolean> {
		const out = await this.runTool(buildInfoArgs(files.map((f) => path.join(base, f.path)), pages));
		const est = out === null ? null : parseEstimate(out);
		if (!est) return true;                       // an estimate is a convenience: never block a conversion on it
		const text = estimateText(est, serverUp);
		if (!needsConfirmation(est, this.settings.confirmOverMinutes)) {
			new Notice(`PDF to Markdown: ${text}`, 8000);
			return true;
		}
		return new Promise<boolean>((resolve) => new ConfirmModal(this.app, files.length, text, resolve).open());
	}

	// ---- starting a conversion ---------------------------------------------------------------

	private async convertOne(file: TFile) {
		if (this.settings.chooseChapters && (await this.offerChapters(file))) return;
		if (this.settings.askForPages) this.askPages(file);
		else void this.convertFiles([file]);
	}

	/**
	 * Check the setup, read the bookmarks and open the picker with everything selected. Returns true when this
	 * handled the request (picker shown, or setup problem reported); false to carry on the old way (no bookmarks,
	 * or an old tool without `outline`).
	 */
	private async offerChapters(file: TFile): Promise<boolean> {
		const base = this.basePath();
		if (!base || this.job || this.starting) return false;   // convertFiles reports these itself
		if (!(await this.checkSetupCached())) return true;
		const outline = await this.readOutline(file, base);
		if (!shouldOfferChapters(true, outline) || !outline) return false;
		new ChapterModal(this.app, file.name, outline, {
			preselectAll: true,
			onSubmit: (pages) => void this.convertFiles([file], pages),
			onPageRange: () => this.askPages(file),
		}).open();
		return true;
	}

	private async readOutline(file: TFile, base: string): Promise<Outline | null> {
		const out = await this.runTool(buildOutlineArgs(path.join(base, file.path)), 120000);
		return out === null ? null : parseOutline(out);
	}

	/** A setup check whose success the next conversion start may reuse for a couple of minutes. */
	private async checkSetupCached(): Promise<boolean> {
		const ready = await this.checkSetup();
		this.setupCache = ready ? { value: ready, at: Date.now() } : null;
		return !!ready;
	}

	private askPages(file: TFile) {
		new PageRangeModal(this.app, file.name, (pages) => void this.convertFiles([file], pages)).open();
	}

	/** Read the PDF's bookmarks and let the user pick chapters; the choice becomes a page list for the normal path. */
	private async chooseChapters(file: TFile) {
		const base = this.basePath();
		if (!base) {
			new Notice("PDF to Markdown only works with a local (desktop) vault.");
			return;
		}
		if (this.job || this.starting) {
			new Notice("PDF to Markdown: a conversion is already running.");
			return;
		}
		if (!(await this.checkSetupCached())) return;
		const outline = await this.readOutline(file, base);
		if (!outline) {
			new Notice("PDF to Markdown: could not read the bookmarks. The installed pdf2md may be older than this plugin: see \"Check the pdf2md setup\".", 10000);
			return;
		}
		new ChapterModal(this.app, file.name, outline, {
			preselectAll: false,
			onSubmit: (pages) => void this.convertFiles([file], pages),
			onPageRange: () => this.askPages(file),
		}).open();
	}

	/** Never throws: every failure is shown as a notice. The slot is claimed at once and released on every early exit. */
	async convertFiles(files: TFile[], pages?: string, opts: RunOptions = {}) {
		if (this.job || this.starting) {
			new Notice("PDF to Markdown: a conversion is already running.");
			return;
		}
		this.starting = true;
		try {
			await this.startJob(files, pages, opts);
		} catch (e) {
			new Notice(`PDF to Markdown: could not start the conversion: ${e instanceof Error ? e.message : String(e)}`, 12000);
		} finally {
			this.starting = false;
		}
	}

	private async startJob(files: TFile[], pages: string | undefined, opts: RunOptions) {
		const base = this.basePath();
		if (!base) {
			new Notice("PDF to Markdown only works with a local (desktop) vault.");
			return;
		}
		if (!files.length) return;
		const cached = this.setupCache && Date.now() - this.setupCache.at < SETUP_CACHE_MS ? this.setupCache.value : null;
		this.setupCache = null;
		const ready = cached ?? (await this.checkSetup());
		if (!ready) return;
		if (!opts.skipEstimate && !(await this.confirmEstimate(files, base, files.length === 1 ? pages : undefined, ready.serverUp))) return;

		let outAbs = opts.outAbs;
		if (outAbs === undefined) {
			const checked = checkOutputFolder(this.settings.outputFolder, this.app.vault.configDir);
			if (!checked.ok) {
				new Notice(`PDF to Markdown: ${checked.reason}`, 10000);
				return;
			}
			if (checked.folder) {
				const folder = normalizePath(checked.folder);
				if (!this.app.vault.getFolderByPath(folder)) await this.app.vault.createFolder(folder);
				outAbs = path.join(base, folder);
			}
		}
		if (opts.beforeRun) await opts.beforeRun();
		const args = buildArgs(
			files.map((f) => path.join(base, f.path)),
			this.settings,
			outAbs,
			files.length === 1 ? pages : undefined,
		);

		this.log = [`$ ${this.settings.pdf2mdPath} ${args.join(" ")}`];
		const proc = spawn(this.settings.pdf2mdPath, args, {
			cwd: base,
			env: this.env(),
			detached: process.platform !== "win32",
		});
		const job: Job = { proc, files, wrote: [], results: [], errors: [], cancelled: false, exited: false, killTimer: null };
		this.job = job;
		this.view = { running: true, files, state: initialState(Date.now(), files.length), wrote: job.wrote, results: job.results,
			pages: files.length === 1 ? retryPages(pages) : undefined };
		this.updateStatus();
		if (this.settings.progressWindow) this.showProgress();
		else new Notice("PDF to Markdown: converting. Click the status bar to see progress.", 6000);

		const feed = lineSplitter((line) => this.onLine(job, line));
		proc.stdout?.on("data", (d: Buffer) => feed(d.toString()));
		proc.stderr?.on("data", (d: Buffer) => feed(d.toString()));
		proc.on("error", (e) => void this.finish(job, null, e.message));
		proc.on("close", (code, signal) => void this.finish(job, code, signal ? `stopped (${signal})` : undefined));
	}

	private onLine(job: Job, line: string) {
		if (!line.trim()) return;
		this.log.push(line);
		if (this.log.length > MAX_LOG_LINES) this.log.splice(1, this.log.length - MAX_LOG_LINES);
		const result = parseResult(line);
		if (result) job.results.push(result);
		const p = parseLine(line);
		if (this.view) this.view.state = reduce(this.view.state, p, Date.now());
		if (p.kind === "wrote") job.wrote.push({ path: p.path, split: p.split });
		if (p.kind === "error") job.errors.push(p.text);
		this.tick();
	}

	private async finish(job: Job, code: number | null, note?: string) {
		job.exited = true;
		if (job.killTimer !== null) {
			window.clearTimeout(job.killTimer);
			job.killTimer = null;
		}
		if (this.job !== job) return;
		this.job = null;
		const total = job.files.length;
		const ok = code === 0 && (job.wrote.length > 0 || job.results.length > 0) && !job.cancelled;
		if (this.view) {
			this.view.running = false;
			this.view.endedAt = Date.now();
			if (!ok) {
				this.view.error = job.cancelled
					? "Cancelled. A later run resumes from the last finished slice."
					: (job.errors[job.errors.length - 1] ?? note ?? `exit code ${code}`);
			}
		}
		this.updateStatus();
		this.modal?.refresh();
		const watching = this.modal !== null;       // the window is open: it shows the result, so no popup or auto-open
		const converted = job.results.length || job.wrote.length;
		const review = reviewItems(job.results).length > 0 ? ' Some results need review: see "Show conversion progress".' : "";
		if (ok) {
			if (watching) return;
			if (total === 1) {
				new Notice(`PDF to Markdown: finished ${job.files[0].name}.${review}`, 8000);
				if (job.results[0]) await this.openAbs(job.results[0].output);
				else if (job.wrote[0]) await this.openAbs(noteToOpen(job.wrote[0]));
			} else {
				new Notice(`PDF to Markdown: converted ${converted} of ${total} PDFs.${review}`, 10000);
			}
			return;
		}
		if (job.cancelled || watching) return;
		const partial = converted ? `${converted} of ${total} converted. ` : "";
		new Notice(`PDF to Markdown failed: ${partial}${this.view?.error}. See "Show the last PDF conversion log".`, 12000);
	}

	// ---- retrying failed pages ---------------------------------------------------------------

	/** The active note, if it records failed pages and the PDF it came from. */
	private retryableNote(): TFile | null {
		const f = this.app.workspace.getActiveFile();
		if (!f || f.extension !== "md") return null;
		const fm = this.app.metadataCache.getFileCache(f)?.frontmatter;
		const failed: unknown = fm?.pages_failed;
		if (!Array.isArray(failed) || failed.length === 0 || typeof fm?.source_pdf !== "string" || !fm.source_pdf) return null;
		return f;
	}

	private async retryFromNote(note: TFile) {
		const base = this.basePath();
		if (!base) return;
		const fm = this.app.metadataCache.getFileCache(note)?.frontmatter;
		const name = typeof fm?.source_pdf === "string" ? baseName(fm.source_pdf) : "";
		if (!name) return;
		const found = this.app.vault.getFiles().filter((f) => isPdf(f) && f.name === name);
		if (!found.length) {
			new Notice(`PDF to Markdown: ${name} is not in this vault.`);
			return;
		}
		const { pick, choices } = pickPdf(note.path, found.map((f) => f.path));
		const byPath = (p: string) => found.find((f) => f.path === p);
		const split = note.name === "00 Index.md" && !!note.parent && !note.parent.isRoot();
		const output = path.join(base, note.path);
		const folder = split && note.parent ? path.join(base, note.parent.path) : null;
		const pages = retryPages(fm?.pages);
		const go = (pdf: TFile) => void this.retryFailed(pdf, output, folder, pages);
		if (pick) {
			const pdf = byPath(pick);
			if (pdf) go(pdf);
		} else {
			new PdfPicker(this.app, (f) => go(f), choices.map(byPath).filter((f): f is TFile => !!f)).open();
		}
	}

	/** Move the previous output to the trash (after confirming) and run the same conversion again at the same place. */
	async retryFailed(pdf: TFile, outputAbs: string, folderAbs: string | null, pages?: string) {
		if (this.job || this.starting) {
			new Notice("PDF to Markdown: a conversion is already running.");
			return;
		}
		const base = this.basePath();
		if (!base) return;
		const plan = retryOutput(outputAbs, folderAbs);
		const rel = this.toVaultRel(plan.trash);
		const target = rel ? this.app.vault.getAbstractFileByPath(rel) : null;
		if (!target) {
			new Notice(`PDF to Markdown: could not find ${plan.trash} in the vault to replace.`, 10000);
			return;
		}
		const what = target instanceof TFolder ? `the folder "${target.path}"` : `the note "${target.path}"`;
		const go = await new Promise<boolean>((resolve) => new TrashConfirmModal(this.app, what, resolve).open());
		if (!go) return;
		await this.convertFiles([pdf], pages, {
			outAbs: plan.out,
			skipEstimate: true,
			beforeRun: () => this.trash(target),
		});
	}

	/** Move to the trash the way the user configured it (the file manager API needs Obsidian 1.6.6; older versions use the system trash). */
	private async trash(target: TAbstractFile) {
		if (requireApiVersion("1.6.6")) await this.app.fileManager.trashFile(target);
		else await this.app.vault.trash(target, true);
	}

	/** Retry from a result in the report window: finds the PDF in the vault, then retries. */
	retryResult(r: ConvertResult, pages?: string) {
		const rel = this.toVaultRel(r.pdf);
		const pdf = rel ? this.app.vault.getAbstractFileByPath(rel) : null;
		if (!(pdf instanceof TFile)) {
			new Notice(`PDF to Markdown: could not find ${baseName(r.pdf)} in the vault.`);
			return;
		}
		void this.retryFailed(pdf, r.output, r.folder, pages);
	}

	// ---- progress window ---------------------------------------------------------------------

	runView(): RunView | null {
		return this.view;
	}

	logTail(n: number): string {
		return this.log.slice(-n).join("\n");
	}

	showProgress() {
		if (!this.view) {
			new Notice("PDF to Markdown: no conversion has run yet.");
			return;
		}
		if (this.modal) return;
		this.modal = new ProgressModal(this.app, this, () => {
			this.modal = null;
		});
		this.modal.open();
	}

	/** Once a second while running: the status bar and the window recompute elapsed time and ETA. */
	private tick() {
		this.updateStatus();
		this.modal?.refresh();
	}

	private updateStatus() {
		if (!this.statusEl) return;
		if (this.view?.running) {
			this.statusEl.setText(statusBarText(this.view.state, Date.now()));
			this.statusEl.show();
		} else {
			this.statusEl.hide();
		}
	}

	/** Open a note the tool wrote (absolute path). If it cannot be found in the vault, say where it was written. */
	async openAbs(abs: string) {
		const rel = this.toVaultRel(abs);
		if (rel) {
			for (let i = 0; i < 10; i++) {
				// the file watcher needs a moment to notice files written by another process
				const f = this.app.vault.getAbstractFileByPath(rel);
				if (f instanceof TFile) {
					await this.app.workspace.getLeaf(false).openFile(f);
					return;
				}
				await new Promise((r) => window.setTimeout(r, 500));
			}
		}
		new Notice(`PDF to Markdown: written to ${abs}`, 12000);
	}

	/** Open a PDF at a page, in a new tab. */
	async openPdfPage(pdfAbs: string, page: number) {
		const rel = this.toVaultRel(pdfAbs);
		if (!rel) {
			new Notice(`PDF to Markdown: ${pdfAbs} is not inside the vault.`);
			return;
		}
		await this.app.workspace.openLinkText(`${rel}#page=${page}`, "", true);
	}

	cancel(quiet = false) {
		const job = this.job;
		if (!job) {
			if (!quiet) new Notice("PDF to Markdown: nothing is running.");
			return;
		}
		if (job.cancelled) return;
		job.cancelled = true;
		this.killTree(job, "SIGTERM");
		if (process.platform !== "win32") {
			// the tool stops its own helpers on SIGTERM; if it has not exited after 10 s, kill the whole group
			job.killTimer = window.setTimeout(() => {
				job.killTimer = null;
				if (!job.exited) this.killTree(job, "SIGKILL");
			}, KILL_AFTER_MS);
		}
		if (!quiet) new Notice("PDF to Markdown: cancelling…");
	}

	private killTree(job: Job, signal: "SIGTERM" | "SIGKILL") {
		const pid = job.proc.pid;
		if (!pid) {
			job.proc.kill(signal);
			return;
		}
		if (process.platform === "win32") {
			execFile("taskkill", ["/pid", String(pid), "/T", "/F"], () => undefined);
			return;
		}
		try {
			process.kill(-pid, signal);
		} catch {
			try {
				job.proc.kill(signal);
			} catch {
				// already gone
			}
		}
	}
}

/** The result of `pdf2md check`: what is ready, and for anything missing the command to run, with a copy button. */
class SetupModal extends Modal {
	constructor(
		app: App,
		private tool: string,
		private checks: SetupCheck[] | null,
		private problem: string | null,
		private warning: string | null,
		private pluginVersion: string,
	) {
		super(app);
	}

	onOpen() {
		this.titleEl.setText("pdf2md setup");
		const el = this.contentEl;
		if (this.problem) el.createEl("p", { cls: "pdf2md-problem", text: this.problem });
		else el.createEl("p", { text: "Everything required is ready." });
		if (this.warning) {
			el.createEl("p", { cls: "pdf2md-warning", text: this.warning });
			el.createEl("p", { text: "You can still convert. To update the tool, run this in a terminal:" });
			this.command(updateCommand(this.pluginVersion));
		}
		if (this.checks === null) {
			el.createEl("p", { text: `Install the tool once in a terminal (then press Check again). The command "${this.tool}" must be on your path, or set its full path in the plugin settings.` });
			this.command("uv tool install git+https://github.com/jyothisable/pdf2md-local");
			this.command("pdf2md setup");
			return;
		}
		for (const c of this.checks) {
			const row = el.createDiv({ cls: "pdf2md-check" });
			row.createSpan({ cls: c.ok ? "pdf2md-ok" : c.required ? "pdf2md-missing" : "pdf2md-optional", text: c.ok ? "Ready" : c.required ? "Missing" : "Not set" });
			row.createSpan({ text: ` ${c.detail}` });
			if (!c.ok && c.fix) this.command(c.fix);
		}
	}

	private command(text: string) {
		const row = this.contentEl.createDiv({ cls: "pdf2md-command" });
		row.createEl("code", { text });
		const copy = row.createEl("button", { text: "Copy" });
		copy.addEventListener("click", () => {
			void navigator.clipboard.writeText(text).then(() => {
				copy.setText("Copied");
				window.setTimeout(() => copy.setText("Copy"), 1500);
			});
		});
	}

	onClose() {
		this.contentEl.empty();
	}
}

/** Shown before a long conversion: pages, estimated time and whether the model server is running. */
class ConfirmModal extends Modal {
	private answered = false;

	constructor(app: App, private count: number, private text: string, private done: (go: boolean) => void) {
		super(app);
	}

	onOpen() {
		this.titleEl.setText(this.count === 1 ? "Convert this PDF?" : `Convert ${this.count} PDFs?`);
		this.contentEl.createEl("p", { text: this.text });
		new Setting(this.contentEl)
			.addButton((b) => b.setButtonText("Convert").setCta().onClick(() => this.answer(true)))
			.addButton((b) => b.setButtonText("Cancel").onClick(() => this.answer(false)));
	}

	private answer(go: boolean) {
		this.answered = true;
		this.close();
		this.done(go);
	}

	onClose() {
		this.contentEl.empty();
		if (!this.answered) this.done(false);
	}
}

/** Confirm replacing earlier output when retrying failed pages. */
class TrashConfirmModal extends Modal {
	private answered = false;

	constructor(app: App, private what: string, private done: (go: boolean) => void) {
		super(app);
	}

	onOpen() {
		this.titleEl.setText("Retry failed pages?");
		this.contentEl.createEl("p", {
			text: `${this.what[0].toUpperCase()}${this.what.slice(1)} will be moved to the trash and written again. ` +
				"If the checkpoint was kept, only the pages that failed are read again; the rest is reused.",
		});
		new Setting(this.contentEl)
			.addButton((b) => b.setButtonText("Move to trash and retry").setCta().onClick(() => this.answer(true)))
			.addButton((b) => b.setButtonText("Cancel").onClick(() => this.answer(false)));
	}

	private answer(go: boolean) {
		this.answered = true;
		this.close();
		this.done(go);
	}

	onClose() {
		this.contentEl.empty();
		if (!this.answered) this.done(false);
	}
}

/** Turn a stream of chunks into whole lines. Progress lines may be split across chunks. */
function lineSplitter(onLine: (line: string) => void): (chunk: string) => void {
	let rest = "";
	return (chunk: string) => {
		const parts = (rest + chunk).split(/\r?\n|\r/);
		rest = parts.pop() ?? "";
		parts.forEach(onLine);
	};
}

/** Every PDF under a folder, in a stable order. */
function collectPdfs(folder: TFolder): TFile[] {
	const out: TFile[] = [];
	const walk = (f: TFolder) => {
		for (const c of f.children) {
			if (c instanceof TFolder) walk(c);
			else if (isPdf(c)) out.push(c);
		}
	};
	walk(folder);
	return out.sort((a, b) => a.path.localeCompare(b.path));
}

class PdfPicker extends FuzzySuggestModal<TFile> {
	constructor(app: App, private onPick: (f: TFile) => void, private items?: TFile[]) {
		super(app);
		this.setPlaceholder(items ? "Several PDFs have this name: choose one" : "Choose a PDF to convert to Markdown");
	}
	getItems(): TFile[] {
		return this.items ?? this.app.vault.getFiles().filter(isPdf);
	}
	getItemText(f: TFile): string {
		return f.path;
	}
	onChooseItem(f: TFile): void {
		this.onPick(f);
	}
}

/** Choose several PDFs from a searchable checklist. */
class MultiPdfModal extends Modal {
	private selected = new Set<TFile>();
	private filter = "";
	private listEl!: HTMLElement;
	private countEl!: HTMLElement;
	private goBtn!: HTMLButtonElement;

	constructor(app: App, private pdfs: TFile[], private onSubmit: (files: TFile[]) => void) {
		super(app);
		this.pdfs = [...pdfs].sort((a, b) => a.path.localeCompare(b.path));
	}

	onOpen() {
		this.titleEl.setText("Convert several PDFs to Markdown");
		const search = this.contentEl.createEl("input", { type: "text", placeholder: "Filter by name or folder…" });
		search.addClass("pdf2md-search");
		search.addEventListener("input", () => {
			this.filter = search.value.toLowerCase();
			this.renderList();
		});
		const bar = this.contentEl.createDiv({ cls: "pdf2md-bar" });
		const all = bar.createEl("button", { text: "Select all shown" });
		all.addEventListener("click", () => {
			this.shown().forEach((f) => this.selected.add(f));
			this.renderList();
		});
		const none = bar.createEl("button", { text: "Clear" });
		none.addEventListener("click", () => {
			this.selected.clear();
			this.renderList();
		});
		this.countEl = bar.createSpan({ cls: "pdf2md-count" });
		this.listEl = this.contentEl.createDiv({ cls: "pdf2md-list" });
		this.goBtn = this.contentEl.createEl("button", { cls: "mod-cta" });
		this.goBtn.addEventListener("click", () => {
			const files = this.pdfs.filter((f) => this.selected.has(f));
			this.close();
			if (files.length) this.onSubmit(files);
		});
		this.renderList();
		search.focus();
	}

	private shown(): TFile[] {
		return this.pdfs.filter((f) => f.path.toLowerCase().includes(this.filter));
	}

	private renderList() {
		this.listEl.empty();
		const shown = this.shown();
		for (const f of shown.slice(0, 300)) {
			const row = this.listEl.createEl("label", { cls: "pdf2md-row" });
			const box = row.createEl("input", { type: "checkbox" });
			box.checked = this.selected.has(f);
			box.addEventListener("change", () => {
				if (box.checked) this.selected.add(f);
				else this.selected.delete(f);
				this.updateButtons();
			});
			row.createSpan({ text: f.path });
		}
		if (shown.length > 300) this.listEl.createDiv({ text: `…and ${shown.length - 300} more: narrow the filter.` });
		this.updateButtons();
	}

	private updateButtons() {
		const n = this.selected.size;
		this.countEl.setText(`${n} selected`);
		this.goBtn.setText(n ? `Convert ${n} PDF${n === 1 ? "" : "s"}` : "Select at least one PDF");
		this.goBtn.disabled = n === 0;
	}

	onClose() {
		this.contentEl.empty();
	}
}

/** Ask which pages to convert. Empty means all of them. */
class PageRangeModal extends Modal {
	private value = "";

	constructor(app: App, private name: string, private onSubmit: (pages: string) => void) {
		super(app);
	}

	onOpen() {
		this.titleEl.setText(`Convert ${this.name}`);
		const go = () => {
			if (!validPages(this.value)) {
				new Notice("Use a page, a range like 10-40, or a list like 10-40,60-80,95. Leave it empty for all pages.");
				return;
			}
			this.close();
			this.onSubmit(this.value.trim());
		};
		new Setting(this.contentEl)
			.setName("Pages")
			.setDesc("For example 10-40, a single page, or a list such as 10-40,60-80,95. Leave empty to convert the whole PDF.")
			.addText((t) => {
				t.setPlaceholder("all pages").onChange((v) => (this.value = v));
				t.inputEl.addEventListener("keydown", (e) => {
					if (e.key === "Enter") go();
				});
				window.setTimeout(() => t.inputEl.focus(), 0);
			});
		new Setting(this.contentEl)
			.addButton((b) => b.setButtonText("Convert").setCta().onClick(go))
			.addButton((b) => b.setButtonText("Cancel").onClick(() => this.close()));
	}

	onClose() {
		this.contentEl.empty();
	}
}


/** Pick chapters from the PDF's bookmarks as a tree of checkboxes. */
class ChapterModal extends Modal {
	private selected = new Set<number>();
	private boxes: HTMLInputElement[] = [];
	private totalEl!: HTMLElement;
	private goBtn!: HTMLButtonElement;

	/**
	 * `preselectAll`: the normal conversion flow, which starts with everything selected and offers "Convert whole PDF".
	 * `onSubmit(undefined)` means the whole PDF, with no page list.
	 */
	constructor(
		app: App,
		private name: string,
		private outline: Outline,
		private opts: { preselectAll: boolean; onSubmit: (pages: string | undefined) => void; onPageRange: () => void },
	) {
		super(app);
		if (opts.preselectAll) this.selected = new Set(outline.entries.map((_, i) => i));
	}

	onOpen() {
		this.titleEl.setText(this.opts.preselectAll ? `Convert ${this.name}` : `Convert chapters of ${this.name}`);
		const el = this.contentEl;
		const { entries, secPerPage } = this.outline;
		if (!entries.length) {
			el.createEl("p", { text: "This PDF has no bookmarks, so its chapters cannot be listed. You can enter a page range instead." });
			new Setting(el)
				.addButton((b) => b.setButtonText("Enter page range…").setCta().onClick(() => {
					this.close();
					this.opts.onPageRange();
				}))
				.addButton((b) => b.setButtonText("Cancel").onClick(() => this.close()));
			return;
		}
		const bar = el.createDiv({ cls: "pdf2md-bar" });
		const all = bar.createEl("button", { text: "Select all" });
		all.addEventListener("click", () => {
			this.selected = new Set(entries.map((_, i) => i));
			this.sync();
		});
		const none = bar.createEl("button", { text: "Select none" });
		none.addEventListener("click", () => {
			this.selected = new Set();
			this.sync();
		});
		this.totalEl = bar.createSpan({ cls: "pdf2md-count" });
		const list = el.createDiv({ cls: "pdf2md-list pdf2md-chapters" });
		entries.forEach((e, i) => {
			const row = list.createEl("label", { cls: "pdf2md-row pdf2md-chapter" });
			row.style.setProperty("--pdf2md-level", String(e.level - 1));
			const box = row.createEl("input", { type: "checkbox" });
			box.addEventListener("change", () => {
				this.selected = toggleEntry(entries, this.selected, i, box.checked);
				this.sync();
			});
			this.boxes.push(box);
			row.createSpan({ cls: "pdf2md-chapter-title", text: e.title });
			row.createSpan({ cls: "pdf2md-muted pdf2md-chapter-info", text: rowLabel(e, secPerPage) });
		});
		const buttons = el.createDiv({ cls: "pdf2md-buttons" });
		const whole = this.opts.preselectAll;
		if (whole) {
			const all = buttons.createEl("button", { text: "Convert whole PDF", cls: "mod-cta" });
			all.addEventListener("click", () => {
				this.close();
				this.opts.onSubmit(undefined);
			});
			const pages = buttons.createEl("button", { text: "Pages…" });
			pages.addEventListener("click", () => {
				this.close();
				this.opts.onPageRange();
			});
		}
		this.goBtn = buttons.createEl("button", { text: whole ? "Convert selected" : "Convert", cls: whole ? "" : "mod-cta" });
		this.goBtn.addEventListener("click", () => {
			const pages = chapterChoicePages(entries, this.selected);
			if (pages === "") return;
			this.close();
			this.opts.onSubmit(pages);
		});
		const cancel = buttons.createEl("button", { text: "Cancel" });
		cancel.addEventListener("click", () => this.close());
		this.sync();
	}

	/** Update the checkboxes (including the partly selected state), the total and the Convert button. */
	private sync() {
		const { entries, secPerPage } = this.outline;
		this.boxes.forEach((box, i) => {
			const st = checkState(entries, this.selected, i);
			box.checked = st === "checked";
			box.indeterminate = st === "mixed";
		});
		const ranges = selectionRanges(entries, this.selected);
		this.totalEl.setText(totalLabel(ranges, secPerPage));
		this.goBtn.disabled = ranges.length === 0;
	}

	onClose() {
		this.contentEl.empty();
		this.boxes = [];
	}
}

/** Progress bar, stage, elapsed time and ETA, then the result and a review report. Closing it leaves the conversion running; the status bar brings it back. */
class ProgressModal extends Modal {
	private detailsOpen = false;
	private built = false;
	private phase: "running" | "ended" | null = null;
	private titleSet = "";
	private barInner!: HTMLElement;
	private bigEl!: HTMLElement;
	private batchEl!: HTMLElement;
	private pagesEl!: HTMLElement;
	private stageEl!: HTMLElement;
	private timeEl!: HTMLElement;
	private logEl!: HTMLElement;
	private logText = "";
	private resultEl!: HTMLElement;
	private buttonsEl!: HTMLElement;

	constructor(app: App, private plugin: Pdf2MdPlugin, private onGone: () => void) {
		super(app);
	}

	onOpen() {
		this.modalEl.addClass("pdf2md-progress");
		this.refresh();
	}

	onClose() {
		this.contentEl.empty();
		this.built = false;
		this.phase = null;
		this.onGone();
	}

	/** The parts that never change while a job runs are built once. */
	private build() {
		const el = this.contentEl;
		this.batchEl = el.createDiv({ cls: "pdf2md-line" });
		const bar = el.createDiv({ cls: "pdf2md-bar-outer" });
		this.barInner = bar.createDiv({ cls: "pdf2md-bar-inner" });
		this.bigEl = el.createDiv({ cls: "pdf2md-big" });
		this.pagesEl = el.createDiv({ cls: "pdf2md-line" });
		this.stageEl = el.createDiv({ cls: "pdf2md-line pdf2md-muted" });
		this.timeEl = el.createDiv({ cls: "pdf2md-line" });
		const details = el.createEl("details", { cls: "pdf2md-details" });
		details.open = this.detailsOpen;
		details.addEventListener("toggle", () => { this.detailsOpen = details.open; });
		details.createEl("summary", { text: "Details" });
		this.logEl = details.createEl("pre", { cls: "pdf2md-log" });
		this.resultEl = el.createDiv({ cls: "pdf2md-result" });
		this.buttonsEl = el.createDiv({ cls: "pdf2md-buttons" });
		this.built = true;
	}

	/** Called every second and on every line of output: only text and the bar width change. */
	refresh() {
		const v = this.plugin.runView();
		if (!v) return;
		if (!this.built) this.build();
		const now = v.endedAt ?? Date.now();
		const s = v.state;
		const single = v.files.length === 1;
		const failed = !!v.error;
		const done = !v.running && !failed;

		const title = single ? `Converting ${v.files[0].name}` : `Converting ${v.files.length} PDFs`;
		if (title !== this.titleSet) {
			this.titleEl.setText(title);
			this.titleSet = title;
		}
		this.batchEl.toggleClass("pdf2md-hidden", single);
		if (!single) this.batchEl.setText(`PDF ${Math.min(s.pdfIndex, s.pdfTotal)} of ${s.pdfTotal}${s.pdfName ? ` · ${s.pdfName}` : ""}`);

		const pct = done ? 100 : Math.round(fractionOverall(s) * 100);
		const indeterminate = v.running && s.pagesTotal === 0;
		this.barInner.toggleClass("is-indeterminate", indeterminate);
		this.barInner.toggleClass("is-failed", failed);
		if (!indeterminate) this.barInner.style.setProperty("--pdf2md-progress", `${pct}%`);

		const pages = s.pagesTotal > 0 ? `Page ${Math.min(s.pagesDone, s.pagesTotal)} of ${s.pagesTotal}` : "Preparing…";
		this.bigEl.setText(failed ? "Stopped" : done ? "Finished" : `${pct}%`);
		this.pagesEl.toggleClass("pdf2md-hidden", !v.running);
		this.pagesEl.setText(`${pages}${single ? "" : " in this PDF"}`);
		this.stageEl.setText(v.running ? s.stage : failed ? (v.error ?? "") : `${s.wrote} note${s.wrote === 1 ? "" : "s"} written`);
		const elapsed = formatDuration(Math.max(0, (now - s.startedAt) / 1000));
		this.timeEl.setText(v.running ? `Elapsed ${elapsed} · ${etaLabel(s, now)}` : `Took ${elapsed}`);

		const tail = this.plugin.logTail(14);
		if (tail !== this.logText) {
			const atEnd = this.logEl.scrollTop + this.logEl.clientHeight >= this.logEl.scrollHeight - 4;
			const keep = this.logEl.scrollTop;
			this.logEl.setText(tail);
			this.logEl.scrollTop = atEnd ? this.logEl.scrollHeight : keep;
			this.logText = tail;
		}

		const phase = v.running ? "running" : "ended";
		if (phase !== this.phase) {
			this.phase = phase;
			this.renderEnd(v, done, single);
		}
	}

	/** Buttons and the result report: rebuilt only when the job starts or ends. */
	private renderEnd(v: NonNullable<ReturnType<Pdf2MdPlugin["runView"]>>, done: boolean, single: boolean) {
		this.buttonsEl.empty();
		this.resultEl.empty();
		if (v.running) {
			const bg = this.buttonsEl.createEl("button", { text: "Run in background", cls: "mod-cta" });
			bg.addEventListener("click", () => this.close());
			const cancel = this.buttonsEl.createEl("button", { text: "Cancel conversion", cls: "mod-warning" });
			cancel.addEventListener("click", () => this.plugin.cancel());
			return;
		}
		this.renderResults(v);
		const close = this.buttonsEl.createEl("button", { text: "Close", cls: done ? "mod-cta" : "" });
		close.addEventListener("click", () => this.close());
		if (done && single && v.results.length === 0 && v.wrote[0]) {
			const w = v.wrote[0];
			const open = this.buttonsEl.createEl("button", { text: "Open note", cls: "mod-cta" });
			open.addEventListener("click", () => {
				this.close();
				void this.plugin.openAbs(noteToOpen(w));
			});
		}
	}

	private link(parent: HTMLElement, text: string, onClick: () => void) {
		const a = parent.createEl("a", { text, href: "#", cls: "pdf2md-link" });
		a.addEventListener("click", (e) => {
			e.preventDefault();
			onClick();
		});
	}

	private renderResults(v: NonNullable<ReturnType<Pdf2MdPlugin["runView"]>>) {
		const el = this.resultEl;
		const plugin = this.plugin;
		const inVault = (abs: string) => plugin.toVaultRel(abs);
		const done = new Set(v.results.map((r) => inVault(r.pdf)));

		// every written main note, clickable; with an old tool (no RESULT lines) fall back to the "wrote" lines
		const notes = v.results.length
			? v.results.map((r) => ({ abs: r.output, label: baseName(r.folder ?? r.output).replace(/\.md$/i, "") }))
			: v.wrote.map((w) => ({ abs: noteToOpen(w), label: baseName(w.path).replace(/\.md$/i, "") }));
		if (notes.length) {
			el.createEl("h4", { text: notes.length === 1 ? "Written note" : `Written notes (${notes.length})` });
			const ul = el.createEl("ul", { cls: "pdf2md-report-list" });
			for (const n of notes) this.link(ul.createEl("li"), n.label, () => void plugin.openAbs(n.abs));
		}
		const missing = v.results.length ? v.files.filter((f) => !done.has(f.path)) : [];
		if (missing.length) {
			el.createEl("h4", { text: v.error && v.error.startsWith("Cancelled") ? "Not converted" : "Failed" });
			const ul = el.createEl("ul", { cls: "pdf2md-report-list" });
			for (const f of missing) ul.createEl("li", { text: f.path });
		}

		const extras = v.results.map((r) => ({ r, text: enrichmentSummary(r) })).filter((x) => x.text);
		if (extras.length) {
			el.createEl("h4", { text: "Callouts and links" });
			const ul = el.createEl("ul", { cls: "pdf2md-report-list" });
			for (const x of extras) ul.createEl("li", { text: v.results.length > 1 ? `${baseName(x.r.pdf)}: ${x.text}` : (x.text ?? "") });
		}

		el.createEl("h4", { text: "Needs review" });
		const items = reviewItems(v.results);
		if (!items.length) {
			el.createDiv({ cls: "pdf2md-muted", text: "Nothing needs review." });
			return;
		}
		for (const r of items) {
			const box = el.createDiv({ cls: "pdf2md-review" });
			box.createDiv({ cls: "pdf2md-review-title", text: baseName(r.pdf) });
			const ul = box.createEl("ul", { cls: "pdf2md-report-list" });
			const pageLinks = (li: HTMLElement, note: string, page: number) => {
				li.createSpan({ text: " · " });
				this.link(li, "Open note", () => void plugin.openAbs(note));
				li.createSpan({ text: " · " });
				this.link(li, "Open PDF page", () => void plugin.openPdfPage(r.pdf, page));
			};
			for (const p of r.pagesFailed) pageLinks(ul.createEl("li", { text: `Page ${p} could not be read` }), r.output, p);
			for (const f of r.formulaImages) pageLinks(ul.createEl("li", { text: `Page ${f.page}: a formula was kept as a picture` }), f.note, f.page);
			for (const m of r.mathWarnings) {
				const li = ul.createEl("li", { text: `${baseName(m.note)}: ${m.count} maths warning${m.count === 1 ? "" : "s"}` });
				li.createSpan({ text: " · " });
				this.link(li, "Open note", () => void plugin.openAbs(m.note));
			}
			if (r.pagesFailed.length) {
				const retry = box.createEl("button", { text: "Retry failed pages" });
				retry.addEventListener("click", () => {
					this.close();
					plugin.retryResult(r, v.pages);
				});
			}
		}
	}
}

class LogModal extends Modal {
	constructor(app: App, private text: string) {
		super(app);
	}
	onOpen() {
		this.titleEl.setText("PDF conversion log");
		const pre = this.contentEl.createEl("pre", { cls: "pdf2md-log" });
		pre.setText(this.text);
	}
	onClose() {
		this.contentEl.empty();
	}
}

class Pdf2MdSettingTab extends PluginSettingTab {
	constructor(app: App, private plugin: Pdf2MdPlugin) {
		super(app, plugin);
	}

	display(): void {
		const { containerEl } = this;
		containerEl.empty();
		const s = this.plugin.settings;
		const save = async () => this.plugin.saveSettings();

		containerEl.createEl("p", {
			text:
				"Converts PDFs to Markdown on this computer with the pdf2md command-line tool (PaddleOCR-VL through llama.cpp). " +
				"Nothing is uploaded. The plugin does not install anything: set the tool up once in a terminal (see the README), then check it here.",
		});
		new Setting(containerEl).setName("Check the setup")
			.setDesc("Runs pdf2md check and reports what is missing, with the terminal command that fixes it.")
			.addButton((b) => b.setButtonText("Check").onClick(() => void this.plugin.checkSetup(true)));
		new Setting(containerEl).setName("pdf2md command").setDesc("Name or full path of the pdf2md executable.")
			.addText((t) => t.setValue(s.pdf2mdPath).onChange(async (v) => { s.pdf2mdPath = v.trim() || DEFAULT_SETTINGS.pdf2mdPath; await save(); }));

		new Setting(containerEl).setName("Model server").setHeading();
		new Setting(containerEl).setName("Server address").setDesc("Where llama-server listens. Default: http://127.0.0.1:8111/v1")
			.addText((t) => t.setValue(s.paddleServer).onChange(async (v) => { s.paddleServer = v.trim() || DEFAULT_SETTINGS.paddleServer; await save(); }));
		new Setting(containerEl).setName("Start command").setDesc("Run only if nothing answers at that address, and stopped when the conversion ends. Empty = pdf2md serve, which needs llama-server on your PATH.")
			.addText((t) => t.setPlaceholder("pdf2md serve").setValue(s.serveCommand).onChange(async (v) => { s.serveCommand = v; await save(); }));

		new Setting(containerEl).setName("Page reading").setHeading();
		new Setting(containerEl).setName("Page resolution")
			.setDesc("Balanced (180 dpi) is recommended. Tables (288 dpi) is slower but reads table-heavy PDFs better.")
			.addDropdown((d) => d.addOptions({ "2.5": "Balanced (180 dpi)", "4": "Tables (288 dpi)" })
				.setValue(String(s.paddleScale)).onChange(async (v) => { s.paddleScale = Number(v); await save(); }));

		new Setting(containerEl).setName("Output").setHeading();
		new Setting(containerEl).setName("Split books into chapters").setDesc("Writes a folder with an index, front matter, one note per chapter and back matter. Needs numbered chapter headings; otherwise one note is written.")
			.addToggle((t) => t.setValue(s.splitChapters).onChange(async (v) => { s.splitChapters = v; await save(); }));
		new Setting(containerEl).setName("Output folder").setDesc("Vault-relative folder. Empty = next to the PDF.")
			.addText((t) => t.setPlaceholder("e.g. Imports").setValue(s.outputFolder).onChange(async (v) => { s.outputFolder = v; await save(); }));
		new Setting(containerEl).setName("Links to PDF pages").setDesc("Adds [[file.pdf#page=N]] under each heading. Auto = on.")
			.addDropdown((d) => d.addOptions({ auto: "Auto", on: "Always", off: "Never" }).setValue(s.pageLinks).onChange(async (v) => { s.pageLinks = v as PageLinks; await save(); }));
		new Setting(containerEl).setName("Theorem callouts").setDesc("Shows theorems, definitions, proofs and similar paragraphs as callouts. Auto = on for Obsidian-style output, off for plain Markdown.")
			.addDropdown((d) => d.addOptions({ auto: "Auto", on: "On", off: "Off" }).setValue(s.callouts).onChange(async (v) => { s.callouts = v as Tri; await save(); }));
		new Setting(containerEl).setName("Cross-reference links").setDesc("Turns references such as Eq. (3.2), Figure 4.1 or Theorem 2.1 into links to their targets. Auto = on for Obsidian-style output, off for plain Markdown.")
			.addDropdown((d) => d.addOptions({ auto: "Auto", on: "On", off: "Off" }).setValue(s.crossLinks).onChange(async (v) => { s.crossLinks = v as Tri; await save(); }));
		new Setting(containerEl).setName("Keep hidden page markers").setDesc("Keeps an invisible <!-- pdf2md:page=N --> comment at the start of every page. Off by default; the visible page links are not affected.")
			.addToggle((t) => t.setValue(s.keepPageMarkers).onChange(async (v) => { s.keepPageMarkers = v; await save(); }));
		new Setting(containerEl).setName("Show a progress window").setDesc("Opens when a conversion starts, with a progress bar and the time left. Close it to keep working: the conversion continues, and clicking the status bar reopens it.")
			.addToggle((t) => t.setValue(s.progressWindow).onChange(async (v) => { s.progressWindow = v; await save(); }));
		new Setting(containerEl).setName("Ask before long conversions").setDesc("Show the page count and estimated time and ask to confirm when a conversion is expected to take at least this many minutes. 0 = never ask (a short notice is still shown).")
			.addText((t) => t.setValue(String(s.confirmOverMinutes)).onChange(async (v) => { const n = Number(v); s.confirmOverMinutes = Number.isFinite(n) && n >= 0 ? n : DEFAULT_SETTINGS.confirmOverMinutes; await save(); }));
		new Setting(containerEl).setName("Choose chapters before converting").setDesc("When a PDF has bookmarks, pick chapters before converting. Off = convert the whole PDF (or ask for pages if that is on).")
			.addToggle((t) => t.setValue(s.chooseChapters).onChange(async (v) => { s.chooseChapters = v; await save(); }));
		new Setting(containerEl).setName("Ask which pages to convert").setDesc("Prompt for a page range before every single-PDF conversion. The command \"Convert PDF pages…\" always asks.")
			.addToggle((t) => t.setValue(s.askForPages).onChange(async (v) => { s.askForPages = v; await save(); }));

		new Setting(containerEl).setName("Advanced").setHeading();
		new Setting(containerEl).setName("Extra arguments").setDesc("Passed straight to pdf2md, e.g. --no-cache")
			.addText((t) => t.setValue(s.extraArgs).onChange(async (v) => { s.extraArgs = v; await save(); }));
	}
}
