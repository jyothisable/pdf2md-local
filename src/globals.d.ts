// The bundled Python tool is imported as text (esbuild `text` loader).
declare module "*.py" {
	const source: string;
	export default source;
}
