#!/usr/bin/env python3
"""Collect Google Agent Platform pricing and optionally apply it to sub2api.

The Google pricing page is an HTML document, not a stable pricing API. This
collector keeps the source URL, generated timestamp, raw table snapshots, and
normalization notes in the exported JSON so pricing changes can be audited.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html.parser
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_URL = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"
USER_AGENT = "Routia-GooglePricingCollector/0.1"
MONEY_LABELS = (
    "Batch Cache Write",
    "Batch Cache Hit",
    "Batch Input",
    "Batch Output",
    "5m Batch Cache Write",
    "1h Batch Cache Write",
    "5m Cache Write",
    "1h Cache Write",
    "Cache Hit",
    "Input",
    "Output",
)


class PricingHTMLParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[dict[str, str]] = []
        self._heading_tag: str | None = None
        self._heading_text: list[str] = []
        self._table_depth = 0
        self._table_headings: list[dict[str, str]] = []
        self._rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell: list[str] | None = None
        self.tables: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"h1", "h2", "h3", "h4"}:
            self._heading_tag = tag
            self._heading_text = []
        if tag == "table":
            self._table_depth += 1
            self._table_headings = self.headings[-8:]
            self._rows = []
        elif self._table_depth and tag == "tr":
            self._row = []
        elif self._table_depth and tag in {"td", "th"}:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if self._heading_tag == tag:
            text = normalize_space("".join(self._heading_text))
            if text:
                self.headings.append({"level": tag, "text": text})
            self._heading_tag = None
            self._heading_text = []
        if self._table_depth and tag in {"td", "th"} and self._cell is not None:
            self._row.append(normalize_space("".join(self._cell)))
            self._cell = None
        elif self._table_depth and tag == "tr":
            if self._row:
                self._rows.append(self._row)
            self._row = []
        elif tag == "table" and self._table_depth:
            self.tables.append({"headings": self._table_headings, "rows": self._rows})
            self._table_depth -= 1
            self._rows = []

    def handle_data(self, data: str) -> None:
        if self._heading_tag:
            self._heading_text.append(data)
        if self._cell is not None:
            self._cell.append(data)


def normalize_space(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=45) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def parse_tables(html: str) -> list[dict[str, Any]]:
    parser = PricingHTMLParser()
    parser.feed(html)
    return parser.tables


def dollars_per_million_to_per_token(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value / 1_000_000, 15)


def first_money(text: str) -> float | None:
    match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", text)
    if not match:
        return None
    return float(match.group(1))


def labeled_money(text: str, label: str) -> float | None:
    # Labels on Google pages are sometimes concatenated, e.g.
    # "Input: $5.00Output: $25.00".
    label_match = re.search(re.escape(label) + r"\s*:", text, re.I)
    if not label_match:
        return None
    start = label_match.end()
    end = len(text)
    for next_label in MONEY_LABELS:
        if next_label.lower() == label.lower():
            continue
        next_match = re.search(re.escape(next_label) + r"\s*:", text[start:], re.I)
        if next_match:
            end = min(end, start + next_match.start())
    return first_money(text[start:end])


def label_to_id(name: str) -> str:
    base = re.sub(r"\s*\(Deprecated\)\s*", "", name, flags=re.I).strip()
    base = base.replace("Computer Use-Preview", " Computer Use Preview")
    base = base.replace("Flash Image", " Flash Image")
    base = re.sub(r"(?i)(\d)(flash)", r"\1 \2", base)
    base = re.sub(r"(?i)(\d)(pro)", r"\1 \2", base)
    base = base.lower()
    base = re.sub(r"[^a-z0-9.]+", "-", base).strip("-")
    if base.startswith("claude-"):
        base = base.replace(".", "-")
    return base


def compact_price_to_dict(text: str) -> dict[str, float | None]:
    return {
        "input_per_1m": labeled_money(text, "Input"),
        "output_per_1m": labeled_money(text, "Output"),
        "batch_input_per_1m": labeled_money(text, "Batch Input"),
        "batch_output_per_1m": labeled_money(text, "Batch Output"),
        "cache_write_5m_per_1m": labeled_money(text, "5m Cache Write"),
        "cache_write_1h_per_1m": labeled_money(text, "1h Cache Write"),
        "cache_read_per_1m": labeled_money(text, "Cache Hit"),
        "batch_cache_write_5m_per_1m": labeled_money(text, "5m Batch Cache Write"),
        "batch_cache_write_1h_per_1m": labeled_money(text, "1h Batch Cache Write"),
        "batch_cache_read_per_1m": labeled_money(text, "Batch Cache Hit"),
    }


def find_last_heading(table: dict[str, Any], prefix: str | None = None) -> str:
    for heading in reversed(table.get("headings", [])):
        text = heading.get("text", "")
        if prefix is None or text.lower().startswith(prefix.lower()):
            return text
    return ""


def parse_claude_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, table in enumerate(tables):
        rows = table.get("rows") or []
        if not rows or not rows[0] or rows[0][0] != "Model":
            continue
        heading_text = " / ".join(h["text"] for h in table.get("headings", []))
        if "Anthropic" not in heading_text and not any("Claude " in (r[0] if r else "") for r in rows[1:]):
            continue
        if not any("Price (/1M tokens)" in c for c in rows[0]):
            continue
        region = find_last_heading(table)
        if region in {"Anthropic’s Claude models", "Models with regional pricing", "Models with uniform pricing across all regions"}:
            region = "unknown"
        uniform = "Models with uniform pricing across all regions" in heading_text
        for row in rows[1:]:
            if len(row) < 2 or not row[0].startswith("Claude "):
                continue
            low = compact_price_to_dict(row[1])
            high = compact_price_to_dict(row[2]) if len(row) > 2 else {}
            model_name = row[0]
            entries.append({
                "provider": "anthropic",
                "platform": "anthropic",
                "model_label": model_name,
                "model_id": label_to_id(model_name),
                "region": "all" if uniform else region,
                "source_table_index": index,
                "pricing_unit": "usd_per_1m_tokens",
                "tiers": [
                    {
                        "name": "<=200k_input_tokens",
                        "min_input_tokens": 0,
                        "max_input_tokens": 200000,
                        **low,
                    },
                    {
                        "name": ">200k_input_tokens",
                        "min_input_tokens": 200001,
                        "max_input_tokens": None,
                        **high,
                    },
                ],
            })
    return dedupe_entries(entries)


def parse_gemini_token_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, table in enumerate(tables):
        rows = table.get("rows") or []
        if not rows or not rows[0] or rows[0][0] != "Model":
            continue
        header = rows[0]
        if not any("<= 200K input tokens" in c for c in header):
            continue
        if not any("Gemini" in (r[0] if r else "") for r in rows[1:]):
            continue
        heading_text = " / ".join(h["text"] for h in table.get("headings", []))
        service_tier = "standard"
        if "Priority" in " ".join(header):
            service_tier = "priority"
        elif "Flex/Batch" in " ".join(header):
            service_tier = "flex_batch"

        current: dict[str, Any] | None = None
        for row in rows[1:]:
            if len(row) == 1 and row[0].startswith("Gemini "):
                current = {
                    "provider": "google",
                    "platform": "gemini",
                    "model_label": row[0],
                    "model_id": label_to_id(row[0]),
                    "service_tier": service_tier,
                    "source_table_index": index,
                    "pricing_unit": "usd_per_1m_tokens",
                    "source_section": heading_text,
                    "input_per_1m": None,
                    "input_long_per_1m": None,
                    "output_per_1m": None,
                    "output_long_per_1m": None,
                    "cache_read_per_1m": None,
                    "cache_read_long_per_1m": None,
                }
                entries.append(current)
                continue
            if current is None or len(row) < 3:
                continue
            typ = row[0].lower()
            is_audio_only = typ.startswith("audio input") or typ.startswith("input (audio")
            if "input" in typ and not is_audio_only:
                current["input_per_1m"] = first_money(row[1])
                current["input_long_per_1m"] = first_money(row[2])
                if len(row) > 3:
                    current["cache_read_per_1m"] = first_money(row[3])
                if len(row) > 4:
                    current["cache_read_long_per_1m"] = first_money(row[4])
            elif "output" in typ:
                current["output_per_1m"] = first_money(row[1])
                current["output_long_per_1m"] = first_money(row[2])
    filtered = [e for e in entries if e.get("input_per_1m") is not None or e.get("output_per_1m") is not None]
    return dedupe_entries(filtered, key_fields=("model_id", "service_tier", "source_table_index"))


def parse_gemini_flat_token_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, table in enumerate(tables):
        rows = table.get("rows") or []
        if not rows or rows[0] != ["Model", "Type", "Price", "Price with Batch API"]:
            continue
        heading_text = " / ".join(h["text"] for h in table.get("headings", []))
        if "Gemini 2.0" not in heading_text or "Token-based pricing" not in heading_text:
            continue
        current: dict[str, Any] | None = None
        for row in rows[1:]:
            if len(row) == 1 and row[0].startswith("Gemini "):
                current = {
                    "provider": "google",
                    "platform": "gemini",
                    "model_label": row[0],
                    "model_id": label_to_id(row[0]),
                    "service_tier": "standard",
                    "source_table_index": index,
                    "pricing_unit": "usd_per_1m_tokens",
                    "source_section": heading_text,
                    "input_per_1m": None,
                    "input_long_per_1m": None,
                    "output_per_1m": None,
                    "output_long_per_1m": None,
                    "cache_read_per_1m": None,
                    "cache_read_long_per_1m": None,
                }
                entries.append(current)
                continue
            if current is None or len(row) < 2:
                continue
            typ = row[0].lower()
            if typ == "1m input tokens" or typ == "1m input text tokens":
                current["input_per_1m"] = first_money(row[1])
            elif typ == "1m output text tokens":
                current["output_per_1m"] = first_money(row[1])
    filtered = [e for e in entries if e.get("input_per_1m") is not None or e.get("output_per_1m") is not None]
    return dedupe_entries(filtered, key_fields=("model_id", "service_tier", "source_table_index"))


def dedupe_entries(entries: list[dict[str, Any]], key_fields: tuple[str, ...] = ("model_id", "region", "source_table_index")) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for entry in entries:
        key = tuple(entry.get(f) for f in key_fields)
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def to_litellm_pricing(claude: list[dict[str, Any]], gemini: list[dict[str, Any]], region: str = "global") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for entry in claude:
        if entry.get("region") not in {region, "all"}:
            continue
        low = entry["tiers"][0]
        result[entry["model_id"]] = {
            "input_cost_per_token": dollars_per_million_to_per_token(low.get("input_per_1m")),
            "output_cost_per_token": dollars_per_million_to_per_token(low.get("output_per_1m")),
            "cache_creation_input_token_cost": dollars_per_million_to_per_token(low.get("cache_write_5m_per_1m")),
            "cache_creation_input_token_cost_above_1hr": dollars_per_million_to_per_token(low.get("cache_write_1h_per_1m")),
            "cache_read_input_token_cost": dollars_per_million_to_per_token(low.get("cache_read_per_1m")),
            "litellm_provider": "anthropic",
            "mode": "chat",
            "supports_prompt_caching": any(t.get("cache_read_per_1m") is not None for t in entry["tiers"]),
        }
    for entry in gemini:
        if entry.get("service_tier") != "standard":
            continue
        result[entry["model_id"]] = {
            "input_cost_per_token": dollars_per_million_to_per_token(entry.get("input_per_1m")),
            "output_cost_per_token": dollars_per_million_to_per_token(entry.get("output_per_1m")),
            "cache_read_input_token_cost": dollars_per_million_to_per_token(entry.get("cache_read_per_1m")),
            "long_context_input_token_threshold": 200000,
            "long_context_input_cost_multiplier": ratio(entry.get("input_long_per_1m"), entry.get("input_per_1m")),
            "long_context_output_cost_multiplier": ratio(entry.get("output_long_per_1m"), entry.get("output_per_1m")),
            "litellm_provider": "google",
            "mode": "chat",
            "supports_prompt_caching": entry.get("cache_read_per_1m") is not None,
        }
    return result


def ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den in (None, 0):
        return None
    return round(num / den, 8)


def clean_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: clean_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [clean_nulls(v) for v in value]
    return value


def to_channel_pricing(claude: list[dict[str, Any]], gemini: list[dict[str, Any]], region: str = "global") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in claude:
        if entry.get("region") not in {region, "all"}:
            continue
        intervals = []
        for idx, tier in enumerate(entry["tiers"]):
            if tier.get("input_per_1m") is None and tier.get("output_per_1m") is None:
                continue
            intervals.append({
                "min_tokens": tier.get("min_input_tokens") or 0,
                "max_tokens": tier.get("max_input_tokens"),
                "tier_label": tier["name"],
                "input_price": dollars_per_million_to_per_token(tier.get("input_per_1m")),
                "output_price": dollars_per_million_to_per_token(tier.get("output_per_1m")),
                "cache_write_price": dollars_per_million_to_per_token(tier.get("cache_write_5m_per_1m")),
                "cache_read_price": dollars_per_million_to_per_token(tier.get("cache_read_per_1m")),
                "per_request_price": None,
                "sort_order": idx,
            })
        if not intervals:
            continue
        first = intervals[0]
        out.append(clean_nulls({
            "platform": "anthropic",
            "models": [entry["model_id"]],
            "billing_mode": "token",
            "input_price": first.get("input_price"),
            "output_price": first.get("output_price"),
            "cache_write_price": first.get("cache_write_price"),
            "cache_read_price": first.get("cache_read_price"),
            "image_output_price": None,
            "per_request_price": None,
            "intervals": intervals,
        }))
    for entry in gemini:
        if entry.get("service_tier") != "standard":
            continue
        intervals = [
            {
                "min_tokens": 0,
                "max_tokens": 200000,
                "tier_label": "<=200k_input_tokens",
                "input_price": dollars_per_million_to_per_token(entry.get("input_per_1m")),
                "output_price": dollars_per_million_to_per_token(entry.get("output_per_1m")),
                "cache_read_price": dollars_per_million_to_per_token(entry.get("cache_read_per_1m")),
                "per_request_price": None,
                "sort_order": 0,
            },
            {
                "min_tokens": 200001,
                "max_tokens": None,
                "tier_label": ">200k_input_tokens",
                "input_price": dollars_per_million_to_per_token(entry.get("input_long_per_1m")),
                "output_price": dollars_per_million_to_per_token(entry.get("output_long_per_1m")),
                "cache_read_price": dollars_per_million_to_per_token(entry.get("cache_read_long_per_1m")),
                "per_request_price": None,
                "sort_order": 1,
            },
        ]
        intervals = [clean_nulls(i) for i in intervals if i.get("input_price") is not None or i.get("output_price") is not None]
        if not intervals:
            continue
        first = intervals[0]
        out.append(clean_nulls({
            "platform": "gemini",
            "models": [entry["model_id"]],
            "billing_mode": "token",
            "input_price": first.get("input_price"),
            "output_price": first.get("output_price"),
            "cache_write_price": None,
            "cache_read_price": first.get("cache_read_price"),
            "image_output_price": None,
            "per_request_price": None,
            "intervals": intervals,
        }))
    return out


def build_export(source_url: str, html: str, include_raw: bool, region: str) -> dict[str, Any]:
    tables = parse_tables(html)
    claude = parse_claude_tables(tables)
    gemini = dedupe_entries(
        parse_gemini_token_tables(tables) + parse_gemini_flat_token_tables(tables),
        key_fields=("model_id", "service_tier", "source_table_index"),
    )
    exported = {
        "schema": "routia.google_agent_pricing.v1",
        "source_url": source_url,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "region_for_sub2api": region,
        "notes": [
            "Google pricing page is HTML, not an official machine-readable pricing API.",
            "Prices are USD per token after converting from USD per 1M tokens.",
            "sub2api channel cache_write_price uses Google/Claude 5m Cache Write; 1h Cache Write is preserved in normalized metadata and LiteLLM export.",
            "Gemini non-global price variants in table cells are not selected; collector uses the first listed price, normally Global.",
        ],
        "counts": {
            "tables": len(tables),
            "claude_entries": len(claude),
            "gemini_entries": len(gemini),
        },
        "normalized": {
            "claude": claude,
            "gemini": gemini,
        },
        "sub2api": {
            "litellm_model_pricing": clean_nulls(to_litellm_pricing(claude, gemini, region=region)),
            "channel_model_pricing": to_channel_pricing(claude, gemini, region=region),
        },
    }
    if include_raw:
        exported["raw_tables"] = tables
    return clean_nulls(exported)


def http_json(method: str, url: str, payload: dict[str, Any] | None = None, token: str | None = None) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {body[:1000]}") from exc
    parsed = json.loads(body) if body.strip() else None
    if isinstance(parsed, dict) and "data" in parsed:
        return parsed["data"]
    return parsed


def apply_to_channel(base_url: str, email: str, password: str, channel_id: int, pricing: list[dict[str, Any]], dry_run: bool) -> None:
    base = base_url.rstrip("/")
    login = http_json("POST", f"{base}/api/v1/auth/login", {"email": email, "password": password})
    token = login.get("access_token") or login.get("token")
    if not token:
        raise RuntimeError("login succeeded but access token was not found")
    channel = http_json("GET", f"{base}/api/v1/admin/channels/{channel_id}", token=token)
    existing = channel.get("model_pricing") or []
    merged = merge_pricing(existing, pricing)
    payload = {
        "name": channel.get("name"),
        "description": channel.get("description"),
        "status": channel.get("status"),
        "group_ids": channel.get("group_ids") or [],
        "model_pricing": merged,
        "model_mapping": channel.get("model_mapping") or {},
        "billing_model_source": channel.get("billing_model_source") or "channel_mapped",
        "restrict_models": channel.get("restrict_models", False),
        "features_config": channel.get("features_config") or {},
        "apply_pricing_to_account_stats": channel.get("apply_pricing_to_account_stats", False),
        "account_stats_pricing_rules": channel.get("account_stats_pricing_rules") or [],
    }
    if dry_run:
        print(json.dumps({
            "dry_run": True,
            "channel_id": channel_id,
            "channel_name": channel.get("name"),
            "existing_model_pricing": len(existing),
            "incoming_model_pricing": len(pricing),
            "merged_model_pricing": len(merged),
            "sample": merged[:5],
        }, ensure_ascii=False, indent=2))
        return
    updated = http_json("PUT", f"{base}/api/v1/admin/channels/{channel_id}", payload, token=token)
    print(json.dumps({"applied": True, "channel_id": channel_id, "model_pricing_count": len(updated.get("model_pricing") or merged)}, ensure_ascii=False, indent=2))


def merge_pricing(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    incoming_keys = {(p.get("platform"), tuple(p.get("models") or [])) for p in incoming}
    kept = [p for p in existing if (p.get("platform"), tuple(p.get("models") or [])) not in incoming_keys]
    return kept + incoming


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Collect Google Agent Platform pricing into Routia/sub2api JSON.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--html", help="Use a local downloaded HTML file instead of fetching --url")
    parser.add_argument("--out", default="google-agent-pricing.json", help="Output JSON path")
    parser.add_argument("--region", default="global", help="Region to select for sub2api exports, e.g. global/us-east5/europe-west1")
    parser.add_argument("--include-raw", action="store_true", help="Include raw parsed HTML tables in JSON")
    parser.add_argument("--apply-channel-id", type=int, help="Apply generated channel_model_pricing to this sub2api channel ID")
    parser.add_argument("--sub2api-base", default="http://127.0.0.1:8080")
    parser.add_argument("--admin-email")
    parser.add_argument("--admin-password")
    parser.add_argument("--dry-run", action="store_true", help="Print apply payload without updating sub2api")
    args = parser.parse_args(argv)

    if args.html:
        html = Path(args.html).read_text(encoding="utf-8", errors="replace")
    else:
        html = fetch_text(args.url)
    exported = build_export(args.url, html, args.include_raw, args.region)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(exported, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"WROTE {out_path} claude={exported['counts']['claude_entries']} gemini={exported['counts']['gemini_entries']} channel_pricing={len(exported['sub2api']['channel_model_pricing'])}")

    if args.apply_channel_id:
        if not args.admin_email or not args.admin_password:
            raise SystemExit("--admin-email and --admin-password are required with --apply-channel-id")
        apply_to_channel(
            args.sub2api_base,
            args.admin_email,
            args.admin_password,
            args.apply_channel_id,
            exported["sub2api"]["channel_model_pricing"],
            args.dry_run,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
