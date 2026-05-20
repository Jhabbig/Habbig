"""
Curated dataset for the AI Race Dashboard.

Every row carries `as_of` (YYYY-MM) and a `source` label so values can be
verified before relying on them. This file is meant to be hand-edited as the
race evolves — see README.md for the maintenance workflow.

Numbers are intentionally rounded; this dashboard surfaces *trajectory*, not
contract-grade benchmark precision. When in doubt, follow the source link
on the dashboard footer to the primary report.
"""

from __future__ import annotations

# Last review date for the curated dataset as a whole.
DATASET_AS_OF = "2026-01"

# ── Labs ─────────────────────────────────────────────────────────────────────
# `valuation_usd_b` is private-market last round (or public market cap for
# parented labs like DeepMind/Meta AI — clearly noted in `valuation_note`).
LABS = [
    {
        "key": "openai",
        "name": "OpenAI",
        "country": "USA",
        "founded": 2015,
        "lead": "Sam Altman (CEO)",
        "homepage": "https://openai.com",
        "color": "#10a37f",
        "valuation_usd_b": 500,
        "valuation_as_of": "2025-10",
        "valuation_note": "Secondary tender at ~$500B (Reuters, Oct 2025).",
        "headline_model": "GPT-5",
        "compute_note": "Stargate buildout (Oracle/Microsoft); multi-GW class.",
        "open_weights": False,
    },
    {
        "key": "anthropic",
        "name": "Anthropic",
        "country": "USA",
        "founded": 2021,
        "lead": "Dario Amodei (CEO)",
        "homepage": "https://www.anthropic.com",
        "color": "#d97757",
        "valuation_usd_b": 170,
        "valuation_as_of": "2025-09",
        "valuation_note": "Series F led by ICONIQ at ~$170B (Sept 2025).",
        "headline_model": "Claude Opus 4.x",
        "compute_note": "Multi-cloud (AWS Trainium, GCP TPU).",
        "open_weights": False,
    },
    {
        "key": "google_deepmind",
        "name": "Google DeepMind",
        "country": "USA / UK",
        "founded": 2010,
        "lead": "Demis Hassabis (CEO)",
        "homepage": "https://deepmind.google",
        "color": "#4285f4",
        "valuation_usd_b": None,
        "valuation_as_of": "2026-01",
        "valuation_note": "Subsidiary of Alphabet (public; no standalone valuation).",
        "headline_model": "Gemini 2.5 / 3",
        "compute_note": "Vertically integrated TPU stack (TPU v5p / v6).",
        "open_weights": False,
    },
    {
        "key": "xai",
        "name": "xAI",
        "country": "USA",
        "founded": 2023,
        "lead": "Elon Musk (CEO)",
        "homepage": "https://x.ai",
        "color": "#cccccc",
        "valuation_usd_b": 200,
        "valuation_as_of": "2025-11",
        "valuation_note": "Reported $200B round (Bloomberg, Nov 2025).",
        "headline_model": "Grok 4",
        "compute_note": "Colossus 1 (~200K H100s) + Colossus 2 buildout.",
        "open_weights": False,
    },
    {
        "key": "meta",
        "name": "Meta AI / Superintelligence Labs",
        "country": "USA",
        "founded": 2013,
        "lead": "Alexandr Wang (Superintelligence Labs)",
        "homepage": "https://ai.meta.com",
        "color": "#1877f2",
        "valuation_usd_b": None,
        "valuation_as_of": "2026-01",
        "valuation_note": "Subsidiary of Meta (public).",
        "headline_model": "Llama 4",
        "compute_note": "Multi-GW buildout; Hyperion / Prometheus campuses.",
        "open_weights": True,
    },
    {
        "key": "deepseek",
        "name": "DeepSeek",
        "country": "China",
        "founded": 2023,
        "lead": "Liang Wenfeng",
        "homepage": "https://www.deepseek.com",
        "color": "#4d6bfe",
        "valuation_usd_b": None,
        "valuation_as_of": "2026-01",
        "valuation_note": "Owned by High-Flyer Capital; no disclosed valuation.",
        "headline_model": "DeepSeek V3 / R1",
        "compute_note": "Reported H800 cluster; efficiency-focused training.",
        "open_weights": True,
    },
    {
        "key": "alibaba",
        "name": "Alibaba (Qwen)",
        "country": "China",
        "founded": 2023,
        "lead": "Junyang Lin (Qwen lead)",
        "homepage": "https://qwenlm.github.io",
        "color": "#ff6a00",
        "valuation_usd_b": None,
        "valuation_as_of": "2026-01",
        "valuation_note": "Subsidiary of Alibaba (public).",
        "headline_model": "Qwen 3",
        "compute_note": "Domestic Ascend + reported H20 access.",
        "open_weights": True,
    },
    {
        "key": "mistral",
        "name": "Mistral AI",
        "country": "France",
        "founded": 2023,
        "lead": "Arthur Mensch (CEO)",
        "homepage": "https://mistral.ai",
        "color": "#fa520f",
        "valuation_usd_b": 13,
        "valuation_as_of": "2025-09",
        "valuation_note": "ASML-led round at ~€11.7B (Sept 2025).",
        "headline_model": "Mistral Large / Medium 3",
        "compute_note": "EU sovereign-AI focus; partner clouds.",
        "open_weights": True,
    },
]

# ── Benchmarks ───────────────────────────────────────────────────────────────
# Each model row scores against a subset of these. `higher_is_better` is True
# for all current entries; kept explicit so we can add cost/latency later.
BENCHMARKS = [
    {
        "key": "mmlu_pro",
        "name": "MMLU-Pro",
        "what": "Hard multiple-choice across 14 disciplines.",
        "scale": "% accuracy",
        "higher_is_better": True,
    },
    {
        "key": "gpqa_diamond",
        "name": "GPQA Diamond",
        "what": "Graduate-level science Q&A (the 'diamond' subset).",
        "scale": "% accuracy",
        "higher_is_better": True,
    },
    {
        "key": "swe_bench_verified",
        "name": "SWE-bench Verified",
        "what": "Resolves real GitHub issues (verified subset).",
        "scale": "% solved",
        "higher_is_better": True,
    },
    {
        "key": "aime_2024",
        "name": "AIME 2024",
        "what": "American Invitational Math Exam, 30 problems.",
        "scale": "% correct",
        "higher_is_better": True,
    },
    {
        "key": "hle",
        "name": "Humanity's Last Exam",
        "what": "Closed-book expert-curated frontier exam.",
        "scale": "% correct",
        "higher_is_better": True,
    },
    {
        "key": "lmarena_elo",
        "name": "LMArena Elo",
        "what": "Crowd-sourced pairwise preference Elo.",
        "scale": "Elo",
        "higher_is_better": True,
    },
    {
        "key": "livecodebench",
        "name": "LiveCodeBench",
        "what": "Continuously refreshed coding contest problems.",
        "scale": "% solved",
        "higher_is_better": True,
    },
]

# ── Frontier models ──────────────────────────────────────────────────────────
# Scores are best public reported on the model card / lab post. When a lab
# reports both with-tool and base scores we take the base unless flagged.
# `null` means no reported / not applicable. Keep this list to ~12 rows.
MODELS = [
    {
        "name": "GPT-5",
        "lab_key": "openai",
        "released": "2025-08",
        "kind": "Frontier reasoning",
        "context_k": 256,
        "open_weights": False,
        "as_of": "2025-12",
        "source": "OpenAI system card / launch post",
        "scores": {
            "mmlu_pro": 87.0,
            "gpqa_diamond": 89.0,
            "swe_bench_verified": 74.9,
            "aime_2024": 94.6,
            "hle": 25.3,
            "lmarena_elo": 1410,
            "livecodebench": 86.0,
        },
    },
    {
        "name": "OpenAI o3",
        "lab_key": "openai",
        "released": "2025-04",
        "kind": "Reasoning (test-time compute)",
        "context_k": 200,
        "open_weights": False,
        "as_of": "2025-12",
        "source": "OpenAI o3 blog / model card",
        "scores": {
            "mmlu_pro": 85.0,
            "gpqa_diamond": 87.7,
            "swe_bench_verified": 71.7,
            "aime_2024": 91.6,
            "hle": 20.3,
            "lmarena_elo": 1380,
            "livecodebench": 79.0,
        },
    },
    {
        "name": "Claude Opus 4.5",
        "lab_key": "anthropic",
        "released": "2025-11",
        "kind": "Frontier reasoning + agents",
        "context_k": 200,
        "open_weights": False,
        "as_of": "2025-12",
        "source": "Anthropic Claude 4.5 launch post",
        "scores": {
            "mmlu_pro": 87.4,
            "gpqa_diamond": 86.5,
            "swe_bench_verified": 80.9,
            "aime_2024": 92.0,
            "hle": 18.0,
            "lmarena_elo": 1395,
            "livecodebench": 80.0,
        },
    },
    {
        "name": "Claude Sonnet 4.5",
        "lab_key": "anthropic",
        "released": "2025-09",
        "kind": "Workhorse + agents",
        "context_k": 200,
        "open_weights": False,
        "as_of": "2025-12",
        "source": "Anthropic Sonnet 4.5 launch post",
        "scores": {
            "mmlu_pro": 84.3,
            "gpqa_diamond": 83.4,
            "swe_bench_verified": 77.2,
            "aime_2024": 87.0,
            "hle": 13.7,
            "lmarena_elo": 1370,
            "livecodebench": 74.0,
        },
    },
    {
        "name": "Gemini 3 Pro",
        "lab_key": "google_deepmind",
        "released": "2025-11",
        "kind": "Frontier multimodal",
        "context_k": 1000,
        "open_weights": False,
        "as_of": "2025-12",
        "source": "Google Gemini 3 launch",
        "scores": {
            "mmlu_pro": 86.5,
            "gpqa_diamond": 88.0,
            "swe_bench_verified": 76.2,
            "aime_2024": 95.0,
            "hle": 23.5,
            "lmarena_elo": 1420,
            "livecodebench": 82.0,
        },
    },
    {
        "name": "Gemini 2.5 Pro",
        "lab_key": "google_deepmind",
        "released": "2025-03",
        "kind": "Frontier multimodal",
        "context_k": 1000,
        "open_weights": False,
        "as_of": "2025-12",
        "source": "Google Gemini 2.5 model card",
        "scores": {
            "mmlu_pro": 84.1,
            "gpqa_diamond": 84.0,
            "swe_bench_verified": 63.8,
            "aime_2024": 88.0,
            "hle": 18.8,
            "lmarena_elo": 1380,
            "livecodebench": 70.0,
        },
    },
    {
        "name": "Grok 4",
        "lab_key": "xai",
        "released": "2025-07",
        "kind": "Frontier reasoning",
        "context_k": 256,
        "open_weights": False,
        "as_of": "2025-12",
        "source": "xAI Grok 4 launch",
        "scores": {
            "mmlu_pro": 86.6,
            "gpqa_diamond": 87.5,
            "swe_bench_verified": 72.0,
            "aime_2024": 94.0,
            "hle": 25.4,
            "lmarena_elo": 1390,
            "livecodebench": 79.0,
        },
    },
    {
        "name": "Llama 4 Maverick",
        "lab_key": "meta",
        "released": "2025-04",
        "kind": "Open weights, MoE",
        "context_k": 1000,
        "open_weights": True,
        "as_of": "2025-12",
        "source": "Meta Llama 4 launch",
        "scores": {
            "mmlu_pro": 80.5,
            "gpqa_diamond": 69.8,
            "swe_bench_verified": 41.0,
            "aime_2024": 70.0,
            "hle": 5.0,
            "lmarena_elo": 1280,
            "livecodebench": 50.0,
        },
    },
    {
        "name": "DeepSeek V3.1",
        "lab_key": "deepseek",
        "released": "2025-08",
        "kind": "Open weights, MoE",
        "context_k": 128,
        "open_weights": True,
        "as_of": "2025-12",
        "source": "DeepSeek V3.1 release notes",
        "scores": {
            "mmlu_pro": 81.2,
            "gpqa_diamond": 75.0,
            "swe_bench_verified": 66.0,
            "aime_2024": 85.0,
            "hle": 9.5,
            "lmarena_elo": 1330,
            "livecodebench": 65.0,
        },
    },
    {
        "name": "DeepSeek R1",
        "lab_key": "deepseek",
        "released": "2025-01",
        "kind": "Open weights reasoning",
        "context_k": 128,
        "open_weights": True,
        "as_of": "2025-12",
        "source": "DeepSeek R1 paper",
        "scores": {
            "mmlu_pro": 80.0,
            "gpqa_diamond": 71.5,
            "swe_bench_verified": 49.2,
            "aime_2024": 79.8,
            "hle": 8.6,
            "lmarena_elo": 1310,
            "livecodebench": 65.9,
        },
    },
    {
        "name": "Qwen 3 Max",
        "lab_key": "alibaba",
        "released": "2025-09",
        "kind": "Open weights",
        "context_k": 256,
        "open_weights": True,
        "as_of": "2025-12",
        "source": "Qwen 3 release",
        "scores": {
            "mmlu_pro": 81.0,
            "gpqa_diamond": 70.0,
            "swe_bench_verified": 55.0,
            "aime_2024": 80.0,
            "hle": 7.5,
            "lmarena_elo": 1300,
            "livecodebench": 60.0,
        },
    },
    {
        "name": "Mistral Large 3",
        "lab_key": "mistral",
        "released": "2025-07",
        "kind": "Open-ish weights",
        "context_k": 128,
        "open_weights": True,
        "as_of": "2025-12",
        "source": "Mistral Large 3 release",
        "scores": {
            "mmlu_pro": 76.0,
            "gpqa_diamond": 65.0,
            "swe_bench_verified": 38.0,
            "aime_2024": 60.0,
            "hle": 4.5,
            "lmarena_elo": 1245,
            "livecodebench": 45.0,
        },
    },
]

# ── Release timeline ─────────────────────────────────────────────────────────
# A subset of "things people will remember from this era." Sorted ASC.
TIMELINE = [
    {"date": "2022-11-30", "lab_key": "openai", "title": "ChatGPT launches", "blurb": "Public access to GPT-3.5 — kicks off the consumer race."},
    {"date": "2023-03-14", "lab_key": "openai", "title": "GPT-4", "blurb": "Multimodal frontier; ~1.7T-class MoE rumored."},
    {"date": "2023-07-18", "lab_key": "meta", "title": "Llama 2 (open weights)", "blurb": "First broadly-permissive open frontier-ish model."},
    {"date": "2023-12-06", "lab_key": "google_deepmind", "title": "Gemini 1.0", "blurb": "DeepMind + Google Brain merged; first Gemini family."},
    {"date": "2024-03-04", "lab_key": "anthropic", "title": "Claude 3 family", "blurb": "Opus first to credibly contest GPT-4 across benchmarks."},
    {"date": "2024-05-13", "lab_key": "openai", "title": "GPT-4o", "blurb": "Native multimodal voice/vision; ChatGPT free tier upgrade."},
    {"date": "2024-09-12", "lab_key": "openai", "title": "o1-preview", "blurb": "Test-time-compute reasoning becomes a product category."},
    {"date": "2024-12-26", "lab_key": "deepseek", "title": "DeepSeek V3", "blurb": "Open-weights MoE trained for ~$5–6M reported compute."},
    {"date": "2025-01-20", "lab_key": "deepseek", "title": "DeepSeek R1", "blurb": "Open reasoning model — global market shock; Nvidia -17% in a day."},
    {"date": "2025-02-27", "lab_key": "openai", "title": "GPT-4.5 ('Orion')", "blurb": "Last big pretraining-era model; pivot toward reasoning."},
    {"date": "2025-04-05", "lab_key": "meta", "title": "Llama 4", "blurb": "Behemoth/Maverick/Scout MoE family; mixed reception."},
    {"date": "2025-05-22", "lab_key": "anthropic", "title": "Claude 4 family", "blurb": "Opus 4 — sustained agentic coding leadership."},
    {"date": "2025-07-09", "lab_key": "xai", "title": "Grok 4", "blurb": "First broadly competitive xAI frontier model."},
    {"date": "2025-08-07", "lab_key": "openai", "title": "GPT-5", "blurb": "Unified reasoning/non-reasoning; Stargate compute online."},
    {"date": "2025-09-29", "lab_key": "anthropic", "title": "Claude Sonnet 4.5", "blurb": "SOTA on agentic coding; SWE-bench Verified ~77%."},
    {"date": "2025-11-18", "lab_key": "google_deepmind", "title": "Gemini 3", "blurb": "Retakes LMArena #1; long-context multimodal lead."},
    {"date": "2025-11-24", "lab_key": "anthropic", "title": "Claude Opus 4.5", "blurb": "Pushes SWE-bench Verified past 80% on a non-tool track."},
]

# ── Market whitelists ────────────────────────────────────────────────────────
# Curated lists of Polymarket events + Kalshi series to surface as "Featured
# AI markets." Editorial, not algorithmic — each entry is a slug/ticker the
# operator confirmed is the right market. The dashboard pulls the full
# multi-outcome tree per event, so a single entry can surface many Yes/No
# probabilities (e.g. "which lab releases the next frontier model" expands to
# one outcome per lab).
#
# Maintenance: visit polymarket.com / kalshi.com, find the event, copy the
# slug or series ticker, add it here. Bad slugs are silently dropped at fetch
# time; check /api/markets/featured response or /api/sources status panel.

AI_POLY_EVENT_SLUGS = [
    # AGI / superintelligence timing
    "will-agi-arrive-by-2030",
    "agi-by-2030",
    "asi-by-2035",
    # Frontier model races
    "best-ai-model-2026",
    "best-ai-model-of-2026",
    "which-company-will-release-the-best-ai-model-2026",
    "highest-lmarena-score-end-of-2026",
    # Lab milestones
    "openai-1t-valuation-2026",
    "anthropic-200b-valuation-2026",
    "xai-revenue-2026",
    # Industrial / chip
    "nvidia-2t-market-cap-2026",
    "us-export-controls-china-ai-2026",
]

AI_KALSHI_SERIES = [
    "KXAGI",         # AGI declarations / outcomes
    "KXAIMODEL",     # frontier-model questions
    "KXOPENAI",      # OpenAI corporate
    "KXANTHROPIC",   # Anthropic corporate
    "KXNVDA",        # Nvidia outcomes (chip side)
]

# Polymarket keyword filter retained for the "More AI markets" secondary view.
# Anything matching that *isn't* in a curated event surfaces there.
AI_MARKET_KEYWORDS = [
    "openai", "anthropic", "claude", "gpt-", "gpt5", "gpt 5", "chatgpt",
    "gemini", "deepmind", "grok", "xai", "elon ai", "deepseek", "qwen",
    "llama", "mistral",
    "agi", "asi", "superintelligence", "artificial general intelligence",
    "ai model", "frontier model", "lmarena", "lmsys", "chatbot arena",
    "ai chip", "h100", "h200", "blackwell", "tpu",
    "ai bubble", "ai race", "ai capex", "nvidia",
]


def lab_by_key(key: str) -> dict | None:
    for lab in LABS:
        if lab["key"] == key:
            return lab
    return None


def best_score_per_benchmark() -> dict[str, dict]:
    """For each benchmark, return the model + score that currently leads."""
    best: dict[str, dict] = {}
    for m in MODELS:
        for bench_key, score in (m.get("scores") or {}).items():
            if score is None:
                continue
            cur = best.get(bench_key)
            if cur is None or score > cur["score"]:
                best[bench_key] = {
                    "model": m["name"],
                    "lab_key": m["lab_key"],
                    "released": m["released"],
                    "score": score,
                }
    return best
