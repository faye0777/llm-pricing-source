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

Each source keeps multiple layers:

- `normalized`: complete normalized pricing observations from the official source page. This is the audit/reference layer and may include models or service tiers that are not suitable for automatic billing.
- `applicable_channel_prices`: Routia billing-standard prices that can be directly imported into Sub2API channel pricing.
- `sub2api.applicable_channel_prices`: compatibility mirror for Sub2API importers.
- `sub2api.channel_model_pricing`: legacy compatibility mirror for older Sub2API importers.

Use `applicable_channel_prices` as the authoritative generated layer for Routia channel billing. The collector keeps tiered prices when the official source has different token ranges; it does not use the pricing page as proof that a model is enabled for a specific Google Cloud project.

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
