# Contributing

```bash
npm install && npm test && npm run test:py && npm run build
```

- Python tests need no models. Changes to extraction behaviour should include a regression test.
- Never commit PDFs, converted notes, images or personal paths. Use synthetic or openly licensed PDFs in examples.
- Plugin changes must follow the [Obsidian developer policies](https://docs.obsidian.md/community-directory/developer-policies):
  no installing or updating dependencies from the plugin, no telemetry, no obfuscation.
