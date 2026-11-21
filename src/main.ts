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
} from "obsidian";
import { ChildProcess, execFile, spawn } from "child_process";
import * as os from "os";
import * as path from "path";
import {
	DEFAULT_SETTINGS,
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
} from "./runner";

const MAX_LOG_LINES = 3000;

const isPdf = (f: TAbstractFile): f is TFile => f instanceof TFile && f.extension.toLowerCase() === "pdf";

interface Job {
	proc: ChildProcess;
	files: TFile[];
	wrote: { path: string; split: boolean }[];
	errors: string[];
	cancelled: boolean;
}

/** What the progress window shows. It outlives the process so the result stays readable after it ends. */
interface RunView {
	running: boolean;
	files: TFile[];
	state: ProgressState;
	wrote: { path: string; split: boolean }[];
	error?: string;
	endedAt?: number;
}

export default class Pdf2MdPlugin extends Plugin {
	settings: Settings = { ...DEFAULT_SETTINGS };
	private job: Job | null = null;
	private log: string[] = [];
	private statusEl: HTMLElement | null = null;
	private view: RunView | null = null;
	private modal: ProgressModal | null = null;

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
				if (active && isPdf(active)) this.convertOne(active);
				else new PdfPicker(this.app, (f) => this.convertOne(f)).open();
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
			id: "convert-several",
			name: "Convert several PDFs…",
			callback: () => {
				const pdfs = this.app.vault.getFiles().filter(isPdf);
				if (!pdfs.length) new Notice("PDF to Markdown: this vault has no PDFs.");
				else new MultiPdfModal(this.app, pdfs, (files) => void this.convertFiles(files)).open();
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
							.setTitle("Convert to Markdown (pdf2md-local)")
							.setIcon("file-text")
							.onClick(() => this.convertOne(file)),
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

	// ---- checks before running ---------------------------------------------------------------

	private env() {
		return toolEnv(process.env, os.homedir(), path.delimiter);
	}

	/**
	 * Ask the installed `pdf2md` whether it is ready. The plugin never installs or downloads anything: when something
	 * is missing it only says which terminal command to run.
	 */
	async checkSetup(verbose = false): Promise<boolean> {
		const s = this.settings;
		const out = await new Promise<{ err: Error | null; stdout: string }>((resolve) =>
			execFile(s.pdf2mdPath, ["check", "--json", "--server", s.paddleServer], { env: this.env(), timeout: 60000 },
				(err, stdout) => resolve({ err, stdout: String(stdout) })),
		);
		const check = parseCheck(out.stdout);
		if (!check) {
			new Notice(
				`PDF to Markdown: could not run "${s.pdf2mdPath}". Install it in a terminal (uv tool install pdf2md-local, then ` +
					`pdf2md setup), or set its full path in the plugin settings.`,
				15000,
			);
			return false;
		}
		const problem = setupProblem(check, s.serveCommand.trim() !== "");
		if (problem) {
			new Notice(`PDF to Markdown: ${problem}`, 15000);
			return false;
		}
		if (verbose) {
			const server = check.checks.find((c) => c.id === "server");
			new Notice(
				"PDF to Markdown: ready." + (server?.ok ? "" : " The model server is not running; it is started for each conversion."),
				8000,
			);
		}
		return true;
	}

	// ---- starting a conversion ---------------------------------------------------------------

	private convertOne(file: TFile) {
		if (this.settings.askForPages) this.askPages(file);
		else void this.convertFiles([file]);
	}

	private askPages(file: TFile) {
		new PageRangeModal(this.app, file.name, (pages) => void this.convertFiles([file], pages)).open();
	}

	async convertFiles(files: TFile[], pages?: string) {
		if (this.job) {
			new Notice("PDF to Markdown: a conversion is already running.");
			return;
		}
		const base = this.basePath();
		if (!base) {
			new Notice("PDF to Markdown only works with a local (desktop) vault.");
			return;
		}
		if (!files.length) return;
		if (!(await this.checkSetup())) return;

		let outAbs: string | undefined;
		const raw = this.settings.outputFolder.trim();
		if (raw) {
			const folder = normalizePath(raw);
			if (folder.split("/").includes("..") || path.isAbsolute(raw)) {
				new Notice("PDF to Markdown: the output folder must be a folder inside the vault.", 10000);
				return;
			}
			if (!this.app.vault.getFolderByPath(folder)) await this.app.vault.createFolder(folder);
			outAbs = path.join(base, folder);
		}
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
		const job: Job = { proc, files, wrote: [], errors: [], cancelled: false };
		this.job = job;
		this.view = { running: true, files, state: initialState(Date.now(), files.length), wrote: job.wrote };
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
		const p = parseLine(line);
		if (this.view) this.view.state = reduce(this.view.state, p, Date.now());
		if (p.kind === "wrote") job.wrote.push({ path: p.path, split: p.split });
		if (p.kind === "error") job.errors.push(p.text);
		this.tick();
	}

	private async finish(job: Job, code: number | null, note?: string) {
		if (this.job !== job) return;
		this.job = null;
		const total = job.files.length;
		const ok = code === 0 && job.wrote.length > 0 && !job.cancelled;
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
		if (ok) {
			if (watching) return;
			if (total === 1) {
				new Notice(`PDF to Markdown: finished ${job.files[0].name}.`, 8000);
				await this.openResult(job.wrote[0]);
			} else {
				new Notice(`PDF to Markdown: converted ${job.wrote.length} of ${total} PDFs.`, 10000);
			}
			return;
		}
		if (job.cancelled || watching) return;
		const partial = job.wrote.length ? `${job.wrote.length} of ${total} converted. ` : "";
		new Notice(`PDF to Markdown failed: ${partial}${this.view?.error}. See "Show the last PDF conversion log".`, 12000);
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

	async openNote(w: { path: string; split: boolean }) {
		await this.openResult(w);
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

	private async openResult(w: { path: string; split: boolean }) {
		const base = this.basePath();
		if (!base) return;
		const rel = toVaultPath(base, noteToOpen(w));
		if (!rel) return;
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

	cancel(quiet = false) {
		const job = this.job;
		if (!job) {
			if (!quiet) new Notice("PDF to Markdown: nothing is running.");
			return;
		}
		job.cancelled = true;
		try {
			if (process.platform !== "win32" && job.proc.pid) process.kill(-job.proc.pid, "SIGTERM");
			else job.proc.kill();
		} catch {
			job.proc.kill();
		}
		if (!quiet) new Notice("PDF to Markdown: cancelling…");
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
	constructor(app: App, private onPick: (f: TFile) => void) {
		super(app);
		this.setPlaceholder("Choose a PDF to convert to Markdown");
	}
	getItems(): TFile[] {
		return this.app.vault.getFiles().filter(isPdf);
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
				new Notice("Use a page number or a range like 10-40, or leave it empty for all pages.");
				return;
			}
			this.close();
			this.onSubmit(this.value.trim());
		};
		new Setting(this.contentEl)
			.setName("Pages")
			.setDesc("For example 10-40, or a single page. Leave empty to convert the whole PDF.")
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

/** Progress bar, stage, elapsed time and ETA. Closing it leaves the conversion running; the status bar brings it back. */
class ProgressModal extends Modal {
	private bodyEl!: HTMLElement;
	private detailsOpen = false;
	private logScroll: number | null = null;

	constructor(app: App, private plugin: Pdf2MdPlugin, private onGone: () => void) {
		super(app);
	}

	onOpen() {
		this.modalEl.addClass("pdf2md-progress");
		this.bodyEl = this.contentEl.createDiv();
		this.refresh();
	}

	onClose() {
		this.contentEl.empty();
		this.onGone();
	}

	/** Redraw from the current state. Cheap: called once a second and on every line of output. */
	refresh() {
		const v = this.plugin.runView();
		if (!v || !this.bodyEl) return;
		const now = v.endedAt ?? Date.now();
		const s = v.state;
		const el = this.bodyEl;
		const oldLog = el.querySelector("pre.pdf2md-log");
		if (oldLog) this.logScroll = oldLog.scrollTop;   // the view is rebuilt every second: keep what the user was reading
		el.empty();

		const single = v.files.length === 1;
		this.titleEl.setText(single ? `Converting ${v.files[0].name}` : `Converting ${v.files.length} PDFs`);
		const failed = !!v.error;
		const done = !v.running && !failed;

		if (!single) {
			el.createDiv({ cls: "pdf2md-line", text: `PDF ${Math.min(s.pdfIndex, s.pdfTotal)} of ${s.pdfTotal}${s.pdfName ? ` · ${s.pdfName}` : ""}` });
		}
		const pct = done ? 100 : Math.round(fractionOverall(s) * 100);
		const bar = el.createDiv({ cls: "pdf2md-bar-outer" });
		const inner = bar.createDiv({ cls: "pdf2md-bar-inner" });
		if (v.running && s.pagesTotal === 0) inner.addClass("is-indeterminate");
		else inner.setCssProps({ "--pdf2md-pct": `${pct}%` });
		if (failed) inner.addClass("is-failed");

		const pages = s.pagesTotal > 0 ? `Page ${Math.min(s.pagesDone, s.pagesTotal)} of ${s.pagesTotal}` : "Preparing…";
		el.createDiv({ cls: "pdf2md-big", text: failed ? "Stopped" : done ? "Finished" : `${pct}%` });
		if (v.running) el.createDiv({ cls: "pdf2md-line", text: `${pages}${single ? "" : " in this PDF"}` });
		el.createDiv({ cls: "pdf2md-line pdf2md-muted", text: v.running ? s.stage : failed ? v.error : `${s.wrote} note${s.wrote === 1 ? "" : "s"} written` });
		const elapsed = formatDuration(Math.max(0, (now - s.startedAt) / 1000));
		el.createDiv({
			cls: "pdf2md-line",
			text: v.running ? `Elapsed ${elapsed} · ${etaLabel(s, now)}` : `Took ${elapsed}`,
		});

		const details = el.createEl("details", { cls: "pdf2md-details" });
		details.open = this.detailsOpen;
		details.addEventListener("toggle", () => { this.detailsOpen = details.open; });
		details.createEl("summary", { text: "Details" });
		const log = details.createEl("pre", { cls: "pdf2md-log", text: this.plugin.logTail(14) });
		if (this.detailsOpen) log.scrollTop = this.logScroll ?? log.scrollHeight;

		const buttons = el.createDiv({ cls: "pdf2md-buttons" });
		if (v.running) {
			const bg = buttons.createEl("button", { text: "Run in background", cls: "mod-cta" });
			bg.addEventListener("click", () => this.close());
			const cancel = buttons.createEl("button", { text: "Cancel conversion", cls: "mod-warning" });
			cancel.addEventListener("click", () => this.plugin.cancel());
		} else {
			if (done && single && v.wrote[0]) {
				const open = buttons.createEl("button", { text: "Open note", cls: "mod-cta" });
				open.addEventListener("click", () => {
					this.close();
					void this.plugin.openNote(v.wrote[0]);
				});
			}
			buttons.createEl("button", { text: "Close", cls: done && !single ? "mod-cta" : "" }).addEventListener("click", () => this.close());
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
		new Setting(containerEl).setName("Page resolution")
			.setDesc("Balanced: 180 dpi (recommended). High resolution: 288 dpi, for table-heavy PDFs.")
			.addDropdown((d) => d.addOptions({ "2.5": "Balanced (180 dpi)", "4": "Tables (288 dpi)" })
				.setValue(String(s.paddleScale)).onChange(async (v) => { s.paddleScale = Number(v); await save(); }));
		new Setting(containerEl).setName("Server address").setDesc("Where llama-server listens. Default: http://127.0.0.1:8111/v1")
			.addText((t) => t.setValue(s.paddleServer).onChange(async (v) => { s.paddleServer = v.trim() || DEFAULT_SETTINGS.paddleServer; await save(); }));
		new Setting(containerEl).setName("Start command").setDesc("Run only if nothing answers at that address, and stopped when the conversion ends. Empty = pdf2md serve, which needs llama-server on your PATH.")
			.addText((t) => t.setPlaceholder("pdf2md serve").setValue(s.serveCommand).onChange(async (v) => { s.serveCommand = v; await save(); }));

		new Setting(containerEl).setName("Output").setHeading();
		new Setting(containerEl).setName("Split books into chapters").setDesc("Writes a folder with an index, front matter, one note per chapter and back matter. Needs numbered chapter headings; otherwise one note is written.")
			.addToggle((t) => t.setValue(s.splitChapters).onChange(async (v) => { s.splitChapters = v; await save(); }));
		new Setting(containerEl).setName("Output folder").setDesc("Vault-relative folder. Empty = next to the PDF.")
			.addText((t) => t.setPlaceholder("e.g. Imports").setValue(s.outputFolder).onChange(async (v) => { s.outputFolder = v; await save(); }));
		new Setting(containerEl).setName("Links to PDF pages").setDesc("Adds [[file.pdf#page=N]] under each heading. Auto = on.")
			.addDropdown((d) => d.addOptions({ auto: "auto", on: "always", off: "never" }).setValue(s.pageLinks).onChange(async (v) => { s.pageLinks = v as PageLinks; await save(); }));
		new Setting(containerEl).setName("Show a progress window").setDesc("Opens when a conversion starts, with a progress bar and the time left. Close it to keep working: the conversion continues, and clicking the status bar reopens it.")
			.addToggle((t) => t.setValue(s.progressWindow).onChange(async (v) => { s.progressWindow = v; await save(); }));
		new Setting(containerEl).setName("Ask which pages to convert").setDesc("Prompt for a page range before every single-PDF conversion. The command \"Convert PDF pages…\" always asks.")
			.addToggle((t) => t.setValue(s.askForPages).onChange(async (v) => { s.askForPages = v; await save(); }));

		new Setting(containerEl).setName("Advanced").setHeading();
		new Setting(containerEl).setName("Extra arguments").setDesc("Passed straight to pdf2md, e.g. --no-cache")
			.addText((t) => t.setValue(s.extraArgs).onChange(async (v) => { s.extraArgs = v; await save(); }));
	}
}
