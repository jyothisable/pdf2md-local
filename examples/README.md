# Examples

`sample.typ` is the source of the small CC0 sample PDF that ships inside the package
(`pdf2md_local/data/sample.pdf`, 2 pages: two numbered chapters, display equations, a table and a figure).

Try it after installing:

```bash
pdf2md selftest                                   # converts it and checks the result
# where the sample PDF is, to convert it yourself:
python -c "import pdf2md_local, pathlib; print(pathlib.Path(pdf2md_local.__file__).with_name('data') / 'sample.pdf')"
```

`expected/` is what a conversion with `--split-chapters --flavor plain` produced on the reference machine. OCR output
can differ slightly between machines and runs; if `pdf2md selftest` reports differences, compare with these files before
filing an issue.
