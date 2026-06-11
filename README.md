# LLM Pricing Source

Public JSON pricing feeds for Routia/Sub2API.

This repository runs lightweight collectors with GitHub Actions and publishes generated JSON files to GitHub Pages.

## Published Files

After GitHub Pages is enabled with **Build and deployment -> GitHub Actions**, the generated files are available at:

```text
https://faye0777.github.io/llm-pricing-source/pricing/index.json
https://faye0777.github.io/llm-pricing-source/pricing/google-agent-platform.json
```

## Sub2API Usage

Use the generated JSON URL in the Sub2API pricing source preview/apply APIs.

Recommended mode for full provider pricing sync:

```json
{
  "mode": "replace_platform",
  "url": "https://faye0777.github.io/llm-pricing-source/pricing/google-agent-platform.json"
}
```

Do not store secrets in this repository. GitHub Pages output should be treated as public.
