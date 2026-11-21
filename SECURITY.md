# Security

Report vulnerabilities privately through GitHub's “Report a vulnerability” on the Security tab (not a public issue).
The tool processes PDFs locally and runs a local model server on `127.0.0.1`. Converting connects only to the model
server address you configure. `pdf2md setup` contacts PyPI (Python packages), Hugging Face (the model, a pinned
revision checked by SHA-256) and GitHub (the llama.cpp release list and a pinned release, checked against its published
digest).

The plugin runs the program, start command and extra arguments named in its settings (`data.json`). Commands are run
without a shell, but anyone who can edit a vault's plugin settings can choose what runs: only enable the plugin in
vaults you trust.
