# LLM Pricing Source

Public JSON pricing feeds for Routia/Sub2API.

This repository runs lightweight collectors with GitHub Actions and publishes generated JSON files to GitHub Pages. Collectors stay outside the Sub2API main process; Sub2API only imports the generated JSON URL.

## Structure

```text
collectors/
  google_agent_platform.py
.github/workflows/
  publish-pricing.yml
```

Generated files are published under GitHub Pages:

```text
https://faye0777.github.io/llm-pricing-source/pricing/index.json
https://faye0777.github.io/llm-pricing-source/pricing/google-agent-platform.json
```

## Enable GitHub Pages

In repository settings:

```text
Settings -> Pages -> Build and deployment -> Source: GitHub Actions
```

Then run the `Publish Pricing JSON` workflow manually once from the Actions tab.

## Sub2API Usage

Preview:

```json
{
  "channel_id": 1,
  "mode": "replace_platform",
  "url": "https://faye0777.github.io/llm-pricing-source/pricing/google-agent-platform.json"
}
```

Apply:

```json
{
  "mode": "replace_platform",
  "url": "https://faye0777.github.io/llm-pricing-source/pricing/google-agent-platform.json"
}
```

Use `replace_platform` for full provider sync. Use `merge` only when you want to keep most existing channel pricing and replace matching platform/model entries.

## Security

Do not store secrets in this repository. GitHub Pages output should be treated as public.
