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
	requestUrl,
} from "obsidian";
import { ChildProcess, execFile, spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import toolSource from "../tool/pdf_to_md.py";
import workerSource from "../tool/paddle_worker.py";
import serveSource from "../tool/serve-paddleocr-vl.sh";
import {
	Api,
	CleanMode,
	DEFAULT_SETTINGS,
	InlineMode,
	PageLinks,
	Progress,
	Settings,
	Engine,
	apiBase,
	batchLabel,
	buildArgs,
	clampInt,
	noteToOpen,
	parseLine,
	statusText,
	toVaultPath,
	toolEnv,
	validPages,
} from "./runner";

const SCRIPT_NAME = "pdf_to_md.py";
const WORKER_NAME = "paddle_worker.py";
const SERVE_NAME = "serve-paddleocr-vl.sh";
const MAX_LOG_LINES = 3000;

const isPdf = (f: TAbstractFile): f is TFile => f instanceof TFile && f.extension.toLowerCase() === "pdf";

interface Job {
	proc: ChildProcess;
	files: TFile[];
	wrote: { path: string; split: boolean }[];
	errors: string[];
	progress: Progress | null;
}

export default class Pdf2MdPlugin extends Plugin {
	settings: Settings = { ...DEFAULT_SETTINGS };
	private job: Job | null = null;
	private log: string[] = [];
	private statusEl: HTMLElement | null = null;

	async onload() {
		await this.loadSettings();
		this.statusEl = this.addStatusBarItem();
		this.statusEl.hide();

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
			id: "show-log",
			name: "Show the last PDF conversion log",
			callback: () => new LogModal(this.app, this.log.join("\n") || "No conversion has run yet.").open(),
		});

		this.registerEvent(
			this.app.workspace.on("file-menu", (menu, file) => {
				if (isPdf(file)) {
					menu.addItem((item) =>
						item
							.setTitle("Convert to Markdown (Docling + local model)")
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
		await this.installScript();
	}

	onunload() {
		this.cancel(true);
	}

	async loadSettings() {
		this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
	}

	async saveSettings() {
		await this.saveData(this.settings);
	}

	// ---- bundled tool ------------------------------------------------------------------------

	/** Absolute path of the bundled model-server script, for the settings page. */
	serveScriptPath(): string | null {
		const base = this.basePath();
		const dir = this.manifest.dir;
		return base && dir ? path.join(base, dir, SERVE_NAME) : null;
	}

	private basePath(): string | null {
		const a = this.app.vault.adapter;
		return a instanceof FileSystemAdapter ? a.getBasePath() : null;
	}

	/** Write the bundled Python tool (and its PaddleOCR-VL worker) into the plugin folder; updated with the plugin. */
	private async installScript(): Promise<string | null> {
		const base = this.basePath();
		const dir = this.manifest.dir;
		if (!base || !dir) return null;
		const adapter = this.app.vault.adapter;
		try {
			for (const [name, source] of [[SCRIPT_NAME, toolSource], [WORKER_NAME, workerSource], [SERVE_NAME, serveSource]]) {
				const rel = `${dir}/${name}`;
				if (!(await adapter.exists(rel)) || (await adapter.read(rel)) !== source) await adapter.write(rel, source);
			}
			try {
				fs.chmodSync(path.join(base, dir, SERVE_NAME), 0o755);
			} catch {
				// not fatal: the start command runs it through bash
			}
		} catch (e) {
			new Notice(`PDF to Markdown: could not install the tool (${(e as Error).message})`);
			return null;
		}
		return path.join(base, dir, SCRIPT_NAME);
	}

	// ---- checks before running ---------------------------------------------------------------

	private env() {
		return toolEnv(process.env, os.homedir(), this.settings.apiKey, path.delimiter);
	}

	/** Is a model server reachable, and (for Ollama) does it have the models we need? Shows a notice if not. */
	private async checkServer(api: Api, host: string, models: string[]): Promise<boolean> {
		const fail = (msg: string) => {
			new Notice(`PDF to Markdown: ${msg}`, 12000);
			return false;
		};
		const key = this.settings.apiKey.trim();
		try {
			if (api === "ollama") {
				const res = await requestUrl({ url: `${host.replace(/\/+$/, "")}/api/tags` });
				const names: string[] = (res.json?.models ?? []).map((m: { name: string }) => m.name);
				const missing = models.filter((w) => w && !names.includes(w) && !names.includes(`${w}:latest`));
				if (missing.length) return fail(`Ollama is missing "${missing[0]}". Run: ollama pull ${missing[0]}`);
			} else {
				// any HTTP answer means the server is up; some servers do not implement /models
				await requestUrl({
					url: `${apiBase(host)}/models`,
					throw: false,
					headers: key ? { Authorization: `Bearer ${key}` } : {},
				});
			}
		} catch {
			return fail(
				api === "ollama"
					? `cannot reach Ollama at ${host}. Start it with "ollama serve", or turn off "Use a model server" in the settings.`
					: `cannot reach the model server at ${apiBase(host)}. Start it first, for example: llama-server -m model.gguf --mmproj mmproj.gguf --port 8080`,
			);
		}
		return true;
	}

	private async preflight(): Promise<boolean> {
		const s = this.settings;
		const uvOk = await new Promise<boolean>((resolve) =>
			execFile(s.uvPath, ["--version"], { env: this.env() }, (err) => resolve(!err)),
		);
		if (!uvOk) {
			new Notice(
				`PDF to Markdown: could not run "${s.uvPath}". Install uv (https://docs.astral.sh/uv/) ` +
					`or set its full path in the plugin settings.`,
				12000,
			);
			return false;
		}
		if (s.engine === "paddleocr-vl") {
			// with a start command the tool launches the server itself; otherwise it must already be running
			if (s.serveCommand.trim()) return true;
			return this.checkServer("openai", s.paddleServer.trim() || DEFAULT_SETTINGS.paddleServer, []);
		}
		if (!s.useLlm) return true;
		const needText = s.cleanMode !== "none";
		const vApi: Api = s.visionApi || s.api;
		const vHost = s.visionHost.trim() || s.ollamaHost;
		if (needText && !(await this.checkServer(s.api, s.ollamaHost, [s.textModel]))) return false;
		if (s.useVision) {
			const vModel = s.visionModel.trim() || s.textModel;
			if (!(await this.checkServer(vApi, vHost, [vModel]))) return false;
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
		if (!(await this.preflight())) return;
		const script = await this.installScript();
		if (!script) return;

		let outAbs: string | undefined;
		const folder = this.settings.outputFolder.trim().replace(/^\/+|\/+$/g, "");
		if (folder) {
			outAbs = path.join(base, folder);
			fs.mkdirSync(outAbs, { recursive: true });
		}
		const args = buildArgs(
			script,
			files.map((f) => path.join(base, f.path)),
			this.settings,
			outAbs,
			files.length === 1 ? pages : undefined,
		);

		this.log = [`$ ${this.settings.uvPath} ${args.join(" ")}`];
		const proc = spawn(this.settings.uvPath, args, {
			cwd: base,
			env: this.env(),
			detached: process.platform !== "win32",
		});
		const job: Job = { proc, files, wrote: [], errors: [], progress: null };
		this.job = job;
		const what = files.length === 1 ? files[0].name : `${files.length} PDFs`;
		new Notice(
			`PDF to Markdown: converting ${what}. The first run downloads models and can take several minutes; ` +
				`long books take an hour or more. Progress is in the status bar.`,
			10000,
		);
		this.setStatus("PDF→MD: starting…");

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
		if (p.kind === "pdf") job.progress = p;
		const text = statusText(p, batchLabel(job.progress));
		if (text) this.setStatus(text);
		if (p.kind === "wrote") job.wrote.push({ path: p.path, split: p.split });
		if (p.kind === "error") job.errors.push(p.text);
	}

	private async finish(job: Job, code: number | null, note?: string) {
		if (this.job !== job) return;
		this.job = null;
		this.setStatus(null);
		const total = job.files.length;
		if (code === 0 && job.wrote.length) {
			if (total === 1) {
				new Notice(`PDF to Markdown: finished ${job.files[0].name}.`, 8000);
				await this.openResult(job.wrote[0]);
			} else {
				new Notice(`PDF to Markdown: converted ${job.wrote.length} of ${total} PDFs.`, 10000);
			}
			return;
		}
		const why = job.errors[job.errors.length - 1] ?? note ?? `exit code ${code}`;
		const partial = job.wrote.length ? `${job.wrote.length} of ${total} converted. ` : "";
		new Notice(`PDF to Markdown failed: ${partial}${why}. See "Show the last PDF conversion log".`, 12000);
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
			await new Promise((r) => setTimeout(r, 500));
		}
	}

	cancel(quiet = false) {
		const job = this.job;
		if (!job) {
			if (!quiet) new Notice("PDF to Markdown: nothing is running.");
			return;
		}
		try {
			if (process.platform !== "win32" && job.proc.pid) process.kill(-job.proc.pid, "SIGTERM");
			else job.proc.kill();
		} catch {
			job.proc.kill();
		}
		if (!quiet) new Notice("PDF to Markdown: cancelled. A later run resumes from the last finished slice.");
	}

	private setStatus(text: string | null) {
		if (!this.statusEl) return;
		if (text) {
			this.statusEl.setText(text);
			this.statusEl.show();
		} else {
			this.statusEl.hide();
		}
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
				"Converts PDFs to Obsidian Markdown on this computer with Docling and a local vision model. " +
				"Nothing is sent to any online service unless you point it at one. Requires uv and a model server.",
		});

		new Setting(containerEl).setName("Engine").setHeading();
		new Setting(containerEl).setName("Conversion engine")
			.setDesc("PaddleOCR-VL-1.6 reads whole pages (layout, text, maths, tables) with a small model served by llama.cpp, no Ollama: best on maths and light on memory in my tests, weaker on tables and old scans. Docling + a vision model uses Ollama or any OpenAI-compatible server and is better on tables and headers/footers.")
			.addDropdown((d) => d.addOptions({ docling: "Docling + vision model", "paddleocr-vl": "PaddleOCR-VL-1.6 (llama.cpp)" })
				.setValue(s.engine).onChange(async (v) => { s.engine = v as Engine; await save(); this.display(); }));

		if (s.engine === "paddleocr-vl") {
			new Setting(containerEl).setName("PaddleOCR-VL server").setHeading();
			containerEl.createEl("p", {
				text: "Runs the PaddleOCR-VL-1.6 model with llama.cpp's llama-server. The plugin folder contains serve-paddleocr-vl.sh in the project's tool folder to download the model and start the server; see the README.",
			});
			new Setting(containerEl).setName("Server address").setDesc("Where llama-server listens. Default: http://127.0.0.1:8111/v1")
				.addText((t) => t.setValue(s.paddleServer).onChange(async (v) => { s.paddleServer = v.trim() || DEFAULT_SETTINGS.paddleServer; await save(); }));
			new Setting(containerEl).setName("Start command").setDesc("Optional. If nothing answers at that address, this command is run first and stopped when the conversion ends. The bundled script downloads the model on first use and needs llama-server on your PATH (or LLAMA_SERVER set).")
				.addText((t) => t.setValue(s.serveCommand).onChange(async (v) => { s.serveCommand = v; await save(); }))
				.addButton((b) => b.setButtonText("Use bundled script").onClick(async () => {
					const script = this.plugin.serveScriptPath();
					if (!script) return;
					s.serveCommand = `bash "${script}"`;
					await save();
					this.display();
				}));
		} else {
			this.modelSettings(containerEl);
		}
		this.outputSettings(containerEl);
	}

	private modelSettings(containerEl: HTMLElement): void {
		const s = this.plugin.settings;
		const save = async () => this.plugin.saveSettings();
		new Setting(containerEl).setName("Model server").setHeading();
		new Setting(containerEl).setName("Use a model server").setDesc("Off = Docling only: no equation re-reading, no inline maths, no captions.")
			.addToggle((t) => t.setValue(s.useLlm).onChange(async (v) => { s.useLlm = v; await save(); }));
		new Setting(containerEl).setName("Server type").setDesc("Ollama, or any OpenAI-compatible server such as llama.cpp's llama-server, LM Studio or vLLM.")
			.addDropdown((d) => d.addOptions({ ollama: "Ollama", openai: "OpenAI-compatible" }).setValue(s.api).onChange(async (v) => { s.api = v as Api; await save(); }));
		new Setting(containerEl).setName("Server address").setDesc("Ollama: http://localhost:11434. llama-server: http://localhost:8080")
			.addText((t) => t.setValue(s.ollamaHost).onChange(async (v) => { s.ollamaHost = v.trim() || DEFAULT_SETTINGS.ollamaHost; await save(); }));
		new Setting(containerEl).setName("API key").setDesc("Only if the server needs one. Stored in this plugin's data file in your vault; sent to the tool through the environment, not the command line.")
			.addText((t) => { t.inputEl.type = "password"; t.setValue(s.apiKey).onChange(async (v) => { s.apiKey = v; await save(); }); });
		new Setting(containerEl).setName("Text model").setDesc("Used only for prose cleanup. For Ollama, pull it first, e.g. ollama pull gemma4:12b")
			.addText((t) => t.setValue(s.textModel).onChange(async (v) => { s.textModel = v.trim() || DEFAULT_SETTINGS.textModel; await save(); }));

		new Setting(containerEl).setName("Vision").setHeading();
		new Setting(containerEl).setName("Use vision").setDesc("Re-read equations, rewrite maths paragraphs as LaTeX and (with a chat model) caption figures.")
			.addToggle((t) => t.setValue(s.useVision).onChange(async (v) => { s.useVision = v; await save(); }));
		new Setting(containerEl).setName("Vision model").setDesc("Empty = same as the text model. For llama-server any name works.")
			.addText((t) => t.setValue(s.visionModel).onChange(async (v) => { s.visionModel = v.trim(); await save(); }));
		new Setting(containerEl).setName("Vision server type").setDesc("Only if the vision model runs on a different kind of server than the text model.")
			.addDropdown((d) => d.addOptions({ "": "Same as above", ollama: "Ollama", openai: "OpenAI-compatible" }).setValue(s.visionApi).onChange(async (v) => { s.visionApi = v as "" | Api; await save(); }));
		new Setting(containerEl).setName("Vision server address").setDesc("Empty = same address as above.")
			.addText((t) => t.setValue(s.visionHost).onChange(async (v) => { s.visionHost = v.trim(); await save(); }));
		new Setting(containerEl).setName("Parallel requests").setDesc("3 is the sweet spot on one GPU. For Ollama keep it at or below OLLAMA_NUM_PARALLEL.")
			.addText((t) => t.setValue(String(s.jobs)).onChange(async (v) => { s.jobs = clampInt(Number(v), 1, 16); await save(); }));

	}

	private outputSettings(containerEl: HTMLElement): void {
		const s = this.plugin.settings;
		const save = async () => this.plugin.saveSettings();
		new Setting(containerEl).setName("Output").setHeading();
		new Setting(containerEl).setName("Split books into chapters").setDesc("Writes a folder with an index, front matter, one note per chapter and back matter. Needs numbered chapter headings; otherwise one note is written.")
			.addToggle((t) => t.setValue(s.splitChapters).onChange(async (v) => { s.splitChapters = v; await save(); }));
		new Setting(containerEl).setName("Output folder").setDesc("Vault-relative folder. Empty = next to the PDF.")
			.addText((t) => t.setPlaceholder("e.g. Imports").setValue(s.outputFolder).onChange(async (v) => { s.outputFolder = v; await save(); }));
		new Setting(containerEl).setName("Links to PDF pages").setDesc("Adds [[file.pdf#page=N]] under each heading. Auto = when the PDF is in your vault.")
			.addDropdown((d) => d.addOptions({ auto: "auto", on: "always", off: "never" }).setValue(s.pageLinks).onChange(async (v) => { s.pageLinks = v as PageLinks; await save(); }));
		new Setting(containerEl).setName("Flag equations that look cut off").setDesc("Adds a collapsed callout with the source picture under an equation that may be incomplete.")
			.addToggle((t) => t.setValue(s.equationFlags).onChange(async (v) => { s.equationFlags = v; await save(); }));
		new Setting(containerEl).setName("Ask which pages to convert").setDesc("Prompt for a page range before every single-PDF conversion. The command \"Convert PDF pages…\" always asks.")
			.addToggle((t) => t.setValue(s.askForPages).onChange(async (v) => { s.askForPages = v; await save(); }));

		new Setting(containerEl).setName("Advanced").setHeading();
		if (s.engine !== "paddleocr-vl") {
			new Setting(containerEl).setName("Prose cleanup").setDesc("auto = only chunks that show extraction damage (recommended).")
				.addDropdown((d) => d.addOptions({ auto: "auto", all: "all", none: "none" }).setValue(s.cleanMode).onChange(async (v) => { s.cleanMode = v as CleanMode; await save(); }));
			new Setting(containerEl).setName("Inline maths").setDesc("auto = paragraphs that look mathematical; all = every paragraph (slower, no better in tests).")
				.addDropdown((d) => d.addOptions({ auto: "auto", all: "all", off: "off" }).setValue(s.inlineMath).onChange(async (v) => { s.inlineMath = v as InlineMode; await save(); }));
			new Setting(containerEl).setName("Pages per slice").setDesc("Big PDFs are converted in slices of this many pages to bound memory. 0 = whole file.")
				.addText((t) => t.setValue(String(s.pagesPerBatch)).onChange(async (v) => { s.pagesPerBatch = clampInt(Number(v), 0, 500); await save(); }));
		}
		new Setting(containerEl).setName("uv executable").setDesc("Name or full path of uv.")
			.addText((t) => t.setValue(s.uvPath).onChange(async (v) => { s.uvPath = v.trim() || "uv"; await save(); }));
		new Setting(containerEl).setName("Extra arguments").setDesc("Passed straight to the tool, e.g. --no-ocr")
			.addText((t) => t.setValue(s.extraArgs).onChange(async (v) => { s.extraArgs = v; await save(); }));
	}
}
