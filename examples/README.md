# Examples

`sample.typ` is the source of the small CC0 sample PDF that ships inside the package
(`pdf2md_local/data/sample.pdf`, 2 pages: two numbered chapters, display equations, a table and a figure).

Try it after installing:

```bash
pdf2md selftest                                   # converts the bundled copy and checks the result
```

To convert it yourself, download [`pdf2md_local/data/sample.pdf`](../pdf2md_local/data/sample.pdf) from this
repository, then:

```bash
pdf2md sample.pdf --split-chapters --flavor plain -o out/ --serve-command "pdf2md serve"
```

`expected/` is what that conversion produced on the reference machine. OCR output can differ slightly between machines
and runs; if `pdf2md selftest` reports differences, compare with these files before filing an issue.
