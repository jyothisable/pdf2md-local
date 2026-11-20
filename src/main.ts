import {
	App,
	FileSystemAdapter,
	FuzzySuggestModal,
	Modal,
	Notice,
	Plugin,
	PluginSettingTab,
	Setting,
	TFile,
	requestUrl,
} from "obsidian";
import { ChildProcess, execFile, spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import toolSource from "../tool/pdf_to_md.py";
import {
	CleanMode,
	DEFAULT_SETTINGS,
	InlineMode,
	Progress,
	Settings,
	augmentedPath,
	buildArgs,
	clampInt,
	noteToOpen,
	parseLine,
	statusText,
	toVaultPath,
} from "./runner";

const SCRIPT_NAME = "pdf_to_md.py";
const MAX_LOG_LINES = 3000;

interface Job {
	proc: ChildProcess;
	pdf: TFile;
	wrote?: { path: string; split: boolean };
	lastError?: string;
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
				if (active && active.extension.toLowerCase() === "pdf") void this.convert(active);
				else new PdfPicker(this.app, (f) => void this.convert(f)).open();
			},
		});
		this.addCommand({
			id: "pick-pdf",
			name: "Convert a PDF from the vault…",
			callback: () => new PdfPicker(this.app, (f) => void this.convert(f)).open(),
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
				if (file instanceof TFile && file.extension.toLowerCase() === "pdf") {
					menu.addItem((item) =>
						item
							.setTitle("Convert to Markdown (Docling + Ollama)")
							.setIcon("file-text")
							.onClick(() => void this.convert(file)),
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

	private basePath(): string | null {
		const a = this.app.vault.adapter;
		return a instanceof FileSystemAdapter ? a.getBasePath() : null;
	}

	/** Write the bundled Python tool into the plugin folder (updated whenever the plugin is). */
	private async installScript(): Promise<string | null> {
		const base = this.basePath();
		const dir = this.manifest.dir;
		if (!base || !dir) return null;
		const rel = `${dir}/${SCRIPT_NAME}`;
		const adapter = this.app.vault.adapter;
		try {
			if (!(await adapter.exists(rel)) || (await adapter.read(rel)) !== toolSource) {
				await adapter.write(rel, toolSource);
			}
		} catch (e) {
			new Notice(`PDF to Markdown: could not install the tool (${(e as Error).message})`);
			return null;
		}
		return path.join(base, rel);
	}

	// ---- running -----------------------------------------------------------------------------

	private async preflight(): Promise<boolean> {
		const s = this.settings;
		const env = { ...process.env, PATH: augmentedPath(process.env, os.homedir(), path.delimiter) };
		const uvOk = await new Promise<boolean>((resolve) =>
			execFile(s.uvPath, ["--version"], { env }, (err) => resolve(!err)),
		);
		if (!uvOk) {
			new Notice(
				`PDF to Markdown: could not run "${s.uvPath}". Install uv (https://docs.astral.sh/uv/) ` +
					`or set its full path in the plugin settings.`,
				12000,
			);
			return false;
		}
		if (s.useLlm) {
			try {
				const res = await requestUrl({ url: `${s.ollamaHost.replace(/\/+$/, "")}/api/tags` });
				const names: string[] = (res.json?.models ?? []).map((m: { name: string }) => m.name);
				const wanted = [s.textModel, s.useVision && s.visionModel.trim() ? s.visionModel.trim() : s.textModel];
				const missing = wanted.filter((w) => !names.includes(w) && !names.includes(`${w}:latest`));
				if (missing.length) {
					new Notice(`PDF to Markdown: Ollama is missing "${missing[0]}". Run: ollama pull ${missing[0]}`, 12000);
					return false;
				}
			} catch {
				new Notice(
					`PDF to Markdown: cannot reach Ollama at ${s.ollamaHost}. Start it with "ollama serve", ` +
						`or turn off "Use Ollama" in the settings.`,
					12000,
				);
				return false;
			}
		}
		return true;
	}

	async convert(file: TFile) {
		if (this.job) {
			new Notice("PDF to Markdown: a conversion is already running.");
			return;
		}
		const base = this.basePath();
		if (!base) {
			new Notice("PDF to Markdown only works with a local (desktop) vault.");
			return;
		}
		if (!(await this.preflight())) return;
		const script = await this.installScript();
		if (!script) return;

		let outAbs: string | undefined;
		const folder = this.settings.outputFolder.trim().replace(/^\/+|\/+$/g, "");
		if (folder) {
			outAbs = path.join(base, folder);
			fs.mkdirSync(outAbs, { recursive: true });
		}
		const args = buildArgs(script, path.join(base, file.path), this.settings, outAbs);

		this.log = [`$ ${this.settings.uvPath} ${args.join(" ")}`];
		const proc = spawn(this.settings.uvPath, args, {
			cwd: base,
			env: { ...process.env, PATH: augmentedPath(process.env, os.homedir(), path.delimiter), PYTHONUNBUFFERED: "1" },
			detached: process.platform !== "win32",
		});
		const job: Job = { proc, pdf: file };
		this.job = job;
		new Notice(
			`PDF to Markdown: converting ${file.name}. The first run downloads models and can take several minutes; ` +
				`long books take an hour or more. Progress is in the status bar.`,
			10000,
		);
		this.setStatus("PDF→MD: starting…");

		const feed = lineSplitter((line) => this.onLine(job, line));
		proc.stdout?.on("data", (d: Buffer) => feed(d.toString()));
		proc.stderr?.on("data", (d: Buffer) => feed(d.toString()));
		proc.on("error", (e) => this.finish(job, null, e.message));
		proc.on("close", (code, signal) => this.finish(job, code, signal ? `stopped (${signal})` : undefined));
	}

	private onLine(job: Job, line: string) {
		if (!line.trim()) return;
		this.log.push(line);
		if (this.log.length > MAX_LOG_LINES) this.log.splice(1, this.log.length - MAX_LOG_LINES);
		const p: Progress = parseLine(line);
		const text = statusText(p);
		if (text) this.setStatus(text);
		if (p.kind === "wrote") job.wrote = { path: p.path, split: p.split };
		if (p.kind === "error") job.lastError = p.text;
	}

	private async finish(job: Job, code: number | null, note?: string) {
		if (this.job !== job) return;
		this.job = null;
		this.setStatus(null);
		if (code === 0 && job.wrote) {
			new Notice(`PDF to Markdown: finished ${job.pdf.name}.`, 8000);
			await this.openResult(job.wrote);
		} else {
			const why = job.lastError ?? note ?? `exit code ${code}`;
			new Notice(`PDF to Markdown failed: ${why}. See "Show the last PDF conversion log".`, 12000);
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

class PdfPicker extends FuzzySuggestModal<TFile> {
	constructor(app: App, private onPick: (f: TFile) => void) {
		super(app);
		this.setPlaceholder("Choose a PDF to convert to Markdown");
	}
	getItems(): TFile[] {
		return this.app.vault.getFiles().filter((f) => f.extension.toLowerCase() === "pdf");
	}
	getItemText(f: TFile): string {
		return f.path;
	}
	onChooseItem(f: TFile): void {
		this.onPick(f);
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
				"Converts PDFs to Obsidian Markdown on this computer with Docling and a local Ollama model. " +
				"Nothing is sent to any online service. Requires uv and Ollama.",
		});

		new Setting(containerEl).setName("Ollama").setHeading();
		new Setting(containerEl).setName("Use Ollama").setDesc("Off = Docling only: no equation re-reading, no inline maths, no captions.")
			.addToggle((t) => t.setValue(s.useLlm).onChange(async (v) => { s.useLlm = v; await save(); }));
		new Setting(containerEl).setName("Ollama address")
			.addText((t) => t.setValue(s.ollamaHost).onChange(async (v) => { s.ollamaHost = v.trim() || DEFAULT_SETTINGS.ollamaHost; await save(); }));
		new Setting(containerEl).setName("Text model").setDesc("Used for cleanup. Pull it first, e.g. ollama pull gemma4:12b")
			.addText((t) => t.setValue(s.textModel).onChange(async (v) => { s.textModel = v.trim() || DEFAULT_SETTINGS.textModel; await save(); }));
		new Setting(containerEl).setName("Vision model").setDesc("Reads equations, inline maths and figures from page images. Empty = same as the text model.")
			.addText((t) => t.setValue(s.visionModel).onChange(async (v) => { s.visionModel = v.trim(); await save(); }));
		new Setting(containerEl).setName("Use vision").setDesc("Re-read equations, rewrite maths paragraphs as LaTeX and caption figures.")
			.addToggle((t) => t.setValue(s.useVision).onChange(async (v) => { s.useVision = v; await save(); }));
		new Setting(containerEl).setName("Parallel requests").setDesc("3 is the sweet spot on one GPU. Keep it at or below OLLAMA_NUM_PARALLEL on the server.")
			.addText((t) => t.setValue(String(s.jobs)).onChange(async (v) => { s.jobs = clampInt(Number(v), 1, 16); await save(); }));

		new Setting(containerEl).setName("Output").setHeading();
		new Setting(containerEl).setName("Split books into chapters").setDesc("Writes a folder with an index, front matter, one note per chapter and back matter. Needs numbered chapter headings; otherwise one note is written.")
			.addToggle((t) => t.setValue(s.splitChapters).onChange(async (v) => { s.splitChapters = v; await save(); }));
		new Setting(containerEl).setName("Output folder").setDesc("Vault-relative folder. Empty = next to the PDF.")
			.addText((t) => t.setPlaceholder("e.g. Imports").setValue(s.outputFolder).onChange(async (v) => { s.outputFolder = v; await save(); }));

		new Setting(containerEl).setName("Advanced").setHeading();
		new Setting(containerEl).setName("Prose cleanup").setDesc("auto = only chunks that show extraction damage (recommended).")
			.addDropdown((d) => d.addOptions({ auto: "auto", all: "all", none: "none" }).setValue(s.cleanMode).onChange(async (v) => { s.cleanMode = v as CleanMode; await save(); }));
		new Setting(containerEl).setName("Inline maths").setDesc("auto = paragraphs that look mathematical; all = every paragraph (slower, no better in tests).")
			.addDropdown((d) => d.addOptions({ auto: "auto", all: "all", off: "off" }).setValue(s.inlineMath).onChange(async (v) => { s.inlineMath = v as InlineMode; await save(); }));
		new Setting(containerEl).setName("Pages per slice").setDesc("Big PDFs are converted in slices of this many pages to bound memory. 0 = whole file.")
			.addText((t) => t.setValue(String(s.pagesPerBatch)).onChange(async (v) => { s.pagesPerBatch = clampInt(Number(v), 0, 500); await save(); }));
		new Setting(containerEl).setName("uv executable").setDesc("Name or full path of uv.")
			.addText((t) => t.setValue(s.uvPath).onChange(async (v) => { s.uvPath = v.trim() || "uv"; await save(); }));
		new Setting(containerEl).setName("Extra arguments").setDesc("Passed straight to the tool, e.g. --no-ocr --pages 10-40")
			.addText((t) => t.setValue(s.extraArgs).onChange(async (v) => { s.extraArgs = v; await save(); }));
	}
}
