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
# Per-row fields:
#   ceiling         — effective max (≤100 for % benchmarks; null for unbounded
#                     scales like Elo). Some ceilings are below 100 because the
#                     benchmark contains some unanswerable/mislabeled items
#                     (MMLU-Pro). Estimates — adjust as evidence accumulates.
#   floor           — display floor (0 for %, 1100 for LMArena).
#   human_baseline  — approximate informed-human reference. null when no clean
#                     human baseline exists or isn't directly comparable.
# These power the saturation gauges and capability radars.
BENCHMARKS = [
    {
        "key": "mmlu_pro",
        "name": "MMLU-Pro",
        "what": "Hard multiple-choice across 14 disciplines.",
        "scale": "% accuracy",
        "higher_is_better": True,
        "floor": 0, "ceiling": 92, "human_baseline": 65,
    },
    {
        "key": "gpqa_diamond",
        "name": "GPQA Diamond",
        "what": "Graduate-level science Q&A (the 'diamond' subset).",
        "scale": "% accuracy",
        "higher_is_better": True,
        "floor": 0, "ceiling": 95, "human_baseline": 70,
    },
    {
        "key": "swe_bench_verified",
        "name": "SWE-bench Verified",
        "what": "Resolves real GitHub issues (verified subset).",
        "scale": "% solved",
        "higher_is_better": True,
        "floor": 0, "ceiling": 100, "human_baseline": None,
    },
    {
        "key": "aime_2024",
        "name": "AIME 2024",
        "what": "American Invitational Math Exam, 30 problems.",
        "scale": "% correct",
        "higher_is_better": True,
        "floor": 0, "ceiling": 100, "human_baseline": 50,
    },
    {
        "key": "hle",
        "name": "Humanity's Last Exam",
        "what": "Closed-book expert-curated frontier exam.",
        "scale": "% correct",
        "higher_is_better": True,
        "floor": 0, "ceiling": 50, "human_baseline": 5,
    },
    {
        "key": "lmarena_elo",
        "name": "LMArena Elo",
        "what": "Crowd-sourced pairwise preference Elo.",
        "scale": "Elo",
        "higher_is_better": True,
        "floor": 1100, "ceiling": None, "human_baseline": None,
    },
    {
        "key": "livecodebench",
        "name": "LiveCodeBench",
        "what": "Continuously refreshed coding contest problems.",
        "scale": "% solved",
        "higher_is_better": True,
        "floor": 0, "ceiling": 100, "human_baseline": None,
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

# ── Compute scoreboard ───────────────────────────────────────────────────────
# Per-lab compute posture. Numbers are estimates from public reporting
# (lab announcements, SemiAnalysis, Reuters/Bloomberg supply-chain stories,
# CEO public statements). H100-equivalents are a rough common-denominator —
# for TPU-only labs we annotate `compute_class` instead. Every row carries
# its own `as_of` because compute changes faster than valuations.
COMPUTE = [
    {
        "lab_key": "openai",
        "h100_equivalents_k": 800,            # K = thousands; via MS Azure + Stargate ramp
        "compute_class": "H100-eq + GB200",
        "flagship_cluster": "Stargate Abilene (multi-GW phased)",
        "capex_2025_usd_b": 50,
        "as_of": "2025-12",
        "source": "MS/Oracle Stargate announcements; partner press",
    },
    {
        "lab_key": "anthropic",
        "h100_equivalents_k": 400,
        "compute_class": "AWS Trainium + GCP TPU + H100",
        "flagship_cluster": "Project Rainier (AWS, multi-site)",
        "capex_2025_usd_b": None,
        "as_of": "2025-11",
        "source": "AWS Project Rainier disclosures; Anthropic blog",
    },
    {
        "lab_key": "google_deepmind",
        "h100_equivalents_k": None,           # TPU-only; no clean H100-eq number
        "compute_class": "TPU v5p / v6e (Trillium)",
        "flagship_cluster": "GCE/SuperPod fabric (multi-GW)",
        "capex_2025_usd_b": 75,               # parent Alphabet AI capex
        "as_of": "2025-10",
        "source": "Alphabet Q3'25 earnings; Google Cloud Next",
    },
    {
        "lab_key": "xai",
        "h100_equivalents_k": 200,
        "compute_class": "H100 + H200 + GB200",
        "flagship_cluster": "Colossus 1 (Memphis, 200K) → Colossus 2 (~1M target)",
        "capex_2025_usd_b": 20,
        "as_of": "2025-11",
        "source": "Nvidia/Supermicro press; Musk public statements",
    },
    {
        "lab_key": "meta",
        "h100_equivalents_k": 1000,
        "compute_class": "H100 + GB200",
        "flagship_cluster": "Hyperion (LA), Prometheus (OH); 5-GW class roadmap",
        "capex_2025_usd_b": 65,
        "as_of": "2025-10",
        "source": "Meta Q3'25 earnings; Zuckerberg public roadmap",
    },
    {
        "lab_key": "deepseek",
        "h100_equivalents_k": 50,             # incl. H800 stockpile
        "compute_class": "H800 + H100 (constrained)",
        "flagship_cluster": "High-Flyer Fire-Flyer cluster",
        "capex_2025_usd_b": None,
        "as_of": "2025-09",
        "source": "DeepSeek V3 paper; SemiAnalysis estimates",
    },
    {
        "lab_key": "alibaba",
        "h100_equivalents_k": None,
        "compute_class": "Ascend 910B + reported H20",
        "flagship_cluster": "Alibaba Cloud regional zones",
        "capex_2025_usd_b": 53,               # 3-yr $53B commitment, prorated
        "as_of": "2025-02",
        "source": "Alibaba 3-yr AI capex announcement (Feb 2025)",
    },
    {
        "lab_key": "mistral",
        "h100_equivalents_k": 20,
        "compute_class": "H100 (partner clouds)",
        "flagship_cluster": "Scaleway / OVHcloud partnerships",
        "capex_2025_usd_b": None,
        "as_of": "2025-09",
        "source": "Mistral press; partner cloud announcements",
    },
]

# ── Export-control timeline ──────────────────────────────────────────────────
# US chip-export rules + responses. Separate from MODELS timeline because the
# cadence and audience differ — these matter for compute distribution, not
# capability.
EXPORT_CONTROLS = [
    {"date": "2022-10-07", "scope": "US BIS",  "title": "Initial advanced-chip controls", "blurb": "A100/H100 banned from China; ushers in the chip-race era."},
    {"date": "2023-10-17", "scope": "US BIS",  "title": "Tightening rules", "blurb": "H800/A800 (China-spec downclocks) banned; performance-density caps."},
    {"date": "2024-03-29", "scope": "China",   "title": "Domestic-substitution push", "blurb": "Huawei Ascend 910B ramp; SMIC 5nm-class yield reports."},
    {"date": "2024-12-02", "scope": "US BIS",  "title": "HBM + tool controls", "blurb": "HBM2e+ + advanced lithography tools added to entity list."},
    {"date": "2025-01-13", "scope": "US BIS",  "title": "AI Diffusion rule (tier framework)", "blurb": "Three-tier export framework; allies get GPU caps without licenses."},
    {"date": "2025-05-13", "scope": "US BIS",  "title": "AI Diffusion rule rescinded", "blurb": "Trump admin reverses tier framework; case-by-case licensing returns."},
    {"date": "2025-07-22", "scope": "US BIS",  "title": "H20 licensing reopens to China", "blurb": "Nvidia H20 export licenses granted again under conditions."},
    {"date": "2025-10-30", "scope": "US BIS",  "title": "New compute-cap framework", "blurb": "Threshold-based controls on next-gen chips above defined FLOP/s density."},
]

# ── Big Tech AI capex tracker ────────────────────────────────────────────────
# Quarterly capex (USD billions) for the AI-spending hyperscalers. Sources are
# each company's earnings releases. We keep ~6 quarters back so a sparkline
# can show the ramp clearly. Update each earnings season.
CAPEX_QUARTERLY = [
    # quarter → {ticker: capex_usd_b}
    {"q": "2024-Q1", "msft": 14.0, "meta": 6.7,  "googl": 12.0, "amzn": 14.9},
    {"q": "2024-Q2", "msft": 19.0, "meta": 8.5,  "googl": 13.2, "amzn": 17.6},
    {"q": "2024-Q3", "msft": 20.0, "meta": 9.2,  "googl": 13.1, "amzn": 22.6},
    {"q": "2024-Q4", "msft": 22.6, "meta": 14.8, "googl": 14.3, "amzn": 27.8},
    {"q": "2025-Q1", "msft": 21.4, "meta": 13.2, "googl": 17.2, "amzn": 24.3},
    {"q": "2025-Q2", "msft": 24.2, "meta": 17.0, "googl": 22.4, "amzn": 31.4},
    {"q": "2025-Q3", "msft": 34.9, "meta": 19.4, "googl": 24.0, "amzn": 34.2},
]
CAPEX_TICKERS = [
    {"ticker": "msft",  "name": "Microsoft", "color": "#00a4ef"},
    {"ticker": "googl", "name": "Alphabet",  "color": "#4285f4"},
    {"ticker": "meta",  "name": "Meta",      "color": "#1877f2"},
    {"ticker": "amzn",  "name": "Amazon",    "color": "#ff9900"},
]

# ── Talent flow ──────────────────────────────────────────────────────────────
# Notable researcher / leadership movements. Editorial — additions appreciated
# when a senior person changes labs. `kind` slots:
#   "founder"  — left to start a new lab
#   "hire"     — joined a lab (incl. acqui-hires)
#   "exit"     — left without immediate destination disclosed
#   "return"   — came back to a previous employer
TALENT_MOVES = [
    {"date": "2024-02-13", "name": "Andrej Karpathy",   "from": "openai",     "to": "eureka_labs",        "kind": "founder", "role": "founder"},
    {"date": "2024-03-19", "name": "Mustafa Suleyman",  "from": "inflection", "to": "microsoft",          "kind": "hire",    "role": "CEO of Microsoft AI"},
    {"date": "2024-05-14", "name": "Ilya Sutskever",    "from": "openai",     "to": "safe_superint",      "kind": "founder", "role": "co-founder, chief scientist"},
    {"date": "2024-05-15", "name": "Jan Leike",         "from": "openai",     "to": "anthropic",          "kind": "hire",    "role": "alignment lead"},
    {"date": "2024-08-08", "name": "John Schulman",     "from": "openai",     "to": "anthropic",          "kind": "hire",    "role": "research"},
    {"date": "2024-08-31", "name": "Noam Shazeer",      "from": "character_ai","to": "google_deepmind",   "kind": "return",  "role": "Gemini lead, via $2.7B licensing"},
    {"date": "2024-09-25", "name": "Mira Murati",       "from": "openai",     "to": "thinking_machines",  "kind": "founder", "role": "CEO"},
    {"date": "2024-09-25", "name": "Bob McGrew",        "from": "openai",     "to": "exit",               "kind": "exit",    "role": "Chief Research Officer"},
    {"date": "2024-11-08", "name": "Lilian Weng",       "from": "openai",     "to": "thinking_machines",  "kind": "hire",    "role": "safety research"},
    {"date": "2025-04-30", "name": "Sholto Douglas",    "from": "anthropic",  "to": "anthropic",          "kind": "return",  "role": "promoted to lead Claude training"},
    {"date": "2025-06-10", "name": "Alexandr Wang",     "from": "scale_ai",   "to": "meta",               "kind": "hire",    "role": "Superintelligence Labs lead"},
    {"date": "2025-09-15", "name": "Mark Chen",         "from": "openai",     "to": "openai",             "kind": "return",  "role": "promoted to Chief Research Officer"},
]

# Friendly display labels for `from`/`to` keys not in LABS.
TALENT_ORG_LABELS = {
    "openai":              {"name": "OpenAI", "color": "#10a37f"},
    "anthropic":           {"name": "Anthropic", "color": "#d97757"},
    "google_deepmind":     {"name": "Google DeepMind", "color": "#4285f4"},
    "meta":                {"name": "Meta", "color": "#1877f2"},
    "microsoft":           {"name": "Microsoft", "color": "#00a4ef"},
    "inflection":          {"name": "Inflection AI", "color": "#7c3aed"},
    "character_ai":        {"name": "Character.AI", "color": "#7c3aed"},
    "safe_superint":       {"name": "Safe Superintelligence", "color": "#e879f9"},
    "thinking_machines":   {"name": "Thinking Machines", "color": "#f472b6"},
    "eureka_labs":         {"name": "Eureka Labs", "color": "#fbbf24"},
    "scale_ai":            {"name": "Scale AI", "color": "#0ea5e9"},
    "exit":                {"name": "—", "color": "#6b7280"},
}

# Approximate lab headcount — wide error bars, curate-and-cite.
HEADCOUNT = [
    {"lab_key": "openai",          "people": 3500, "as_of": "2025-10", "source": "Reuters, Information reporting"},
    {"lab_key": "anthropic",       "people": 1500, "as_of": "2025-11", "source": "company hiring page count + press"},
    {"lab_key": "google_deepmind", "people": 6000, "as_of": "2025-09", "source": "Alphabet 10-Q + press, includes Google AI"},
    {"lab_key": "xai",             "people": 1200, "as_of": "2025-10", "source": "xAI press; SF + Memphis hiring waves"},
    {"lab_key": "meta",            "people": 4500, "as_of": "2025-09", "source": "Superintelligence Labs + FAIR + GenAI consolidated"},
    {"lab_key": "deepseek",        "people": 200,  "as_of": "2025-08", "source": "FT profile; deliberately small"},
    {"lab_key": "alibaba",         "people": 1000, "as_of": "2025-08", "source": "Qwen team + DAMO Academy"},
    {"lab_key": "mistral",         "people": 350,  "as_of": "2025-09", "source": "company filings; post-ASML round"},
]

# ── News feeds ───────────────────────────────────────────────────────────────
# RSS feeds for the AI news fan-in. Mix of lab blogs, research feeds, and
# narrative-shaping commentary. Add/remove freely.
NEWS_FEEDS = [
    {"name": "OpenAI",            "url": "https://openai.com/blog/rss.xml",                    "kind": "lab"},
    {"name": "Anthropic",         "url": "https://www.anthropic.com/news/rss.xml",             "kind": "lab"},
    {"name": "Google DeepMind",   "url": "https://deepmind.google/blog/rss.xml",               "kind": "lab"},
    {"name": "HuggingFace blog",  "url": "https://huggingface.co/blog/feed.xml",               "kind": "community"},
    {"name": "arXiv cs.CL",       "url": "http://export.arxiv.org/rss/cs.CL",                  "kind": "research"},
    {"name": "Import AI",         "url": "https://importai.substack.com/feed",                 "kind": "newsletter"},
    {"name": "Stratechery",       "url": "https://stratechery.com/feed/",                      "kind": "newsletter"},
    {"name": "Ben's Bites",       "url": "https://bensbites.beehiiv.com/feed",                 "kind": "newsletter"},
    {"name": "AI Snake Oil",      "url": "https://www.aisnakeoil.com/feed",                    "kind": "newsletter"},
]

# ── Funding rounds ───────────────────────────────────────────────────────────
# Notable equity rounds by lab. `amount_usd_b` is round size; `post_usd_b` is
# post-money valuation. Both USD billions. Curated from press / Reuters /
# Bloomberg. Add new rounds as they close.
FUNDING_ROUNDS = [
    # OpenAI
    {"date": "2019-07-22", "lab_key": "openai",    "round": "Strategic", "amount_usd_b": 1.0,  "post_usd_b": None,  "lead": "Microsoft"},
    {"date": "2023-01-23", "lab_key": "openai",    "round": "Strategic", "amount_usd_b": 10.0, "post_usd_b": 29.0,  "lead": "Microsoft (multi-year)"},
    {"date": "2024-10-02", "lab_key": "openai",    "round": "Tender",    "amount_usd_b": 6.6,  "post_usd_b": 157.0, "lead": "Thrive Capital"},
    {"date": "2025-04-01", "lab_key": "openai",    "round": "Strategic", "amount_usd_b": 40.0, "post_usd_b": 300.0, "lead": "SoftBank"},
    {"date": "2025-10-15", "lab_key": "openai",    "round": "Secondary", "amount_usd_b": 6.6,  "post_usd_b": 500.0, "lead": "Thrive (employee tender)"},
    # Anthropic
    {"date": "2023-09-25", "lab_key": "anthropic", "round": "Strategic", "amount_usd_b": 4.0,  "post_usd_b": 18.0,  "lead": "Amazon"},
    {"date": "2024-03-27", "lab_key": "anthropic", "round": "Strategic", "amount_usd_b": 2.75, "post_usd_b": 18.4,  "lead": "Amazon top-up"},
    {"date": "2024-11-22", "lab_key": "anthropic", "round": "Strategic", "amount_usd_b": 4.0,  "post_usd_b": 40.0,  "lead": "Amazon"},
    {"date": "2025-03-03", "lab_key": "anthropic", "round": "Series E",  "amount_usd_b": 3.5,  "post_usd_b": 61.5,  "lead": "Lightspeed"},
    {"date": "2025-09-02", "lab_key": "anthropic", "round": "Series F",  "amount_usd_b": 13.0, "post_usd_b": 170.0, "lead": "ICONIQ Capital"},
    # xAI
    {"date": "2023-12-29", "lab_key": "xai",       "round": "Series A",  "amount_usd_b": 6.0,  "post_usd_b": 20.0,  "lead": "Sequoia / a16z"},
    {"date": "2024-12-23", "lab_key": "xai",       "round": "Series B",  "amount_usd_b": 6.0,  "post_usd_b": 40.0,  "lead": "Andreessen Horowitz"},
    {"date": "2025-11-04", "lab_key": "xai",       "round": "Strategic", "amount_usd_b": 10.0, "post_usd_b": 200.0, "lead": "Saudi PIF / Valor (reported)"},
    # Mistral
    {"date": "2024-06-11", "lab_key": "mistral",   "round": "Series B",  "amount_usd_b": 0.64, "post_usd_b": 6.2,   "lead": "General Catalyst"},
    {"date": "2025-09-09", "lab_key": "mistral",   "round": "Series C",  "amount_usd_b": 1.94, "post_usd_b": 13.7,  "lead": "ASML"},
]

# ── Public AI-exposed equities ───────────────────────────────────────────────
# Snapshot — refresh weekly from public quotes. `pe` is TTM; `range_52w` =
# [low, high]; `theme` groups in the UI.
AI_STOCKS = [
    {"ticker": "NVDA",  "name": "Nvidia",         "theme": "Chips",       "price": 188.0, "daily_pct":  0.6, "ytd_pct":  39.8, "pe":  53.0, "mkt_cap_t": 4.60, "range_52w": [101.0, 212.0]},
    {"ticker": "AVGO",  "name": "Broadcom",       "theme": "Chips",       "price": 365.0, "daily_pct":  0.4, "ytd_pct":  58.2, "pe":  72.0, "mkt_cap_t": 1.70, "range_52w": [190.0, 380.0]},
    {"ticker": "AMD",   "name": "AMD",            "theme": "Chips",       "price": 142.0, "daily_pct": -0.3, "ytd_pct":  16.7, "pe":  51.0, "mkt_cap_t": 0.23, "range_52w": [ 95.0, 187.0]},
    {"ticker": "TSM",   "name": "TSMC (ADR)",     "theme": "Foundry",     "price": 244.0, "daily_pct":  0.8, "ytd_pct":  38.0, "pe":  31.0, "mkt_cap_t": 1.30, "range_52w": [155.0, 252.0]},
    {"ticker": "MSFT",  "name": "Microsoft",      "theme": "Hyperscaler", "price": 480.0, "daily_pct":  0.2, "ytd_pct":   8.0, "pe":  36.0, "mkt_cap_t": 3.60, "range_52w": [385.0, 510.0]},
    {"ticker": "GOOGL", "name": "Alphabet",       "theme": "Hyperscaler", "price": 192.0, "daily_pct": -0.1, "ytd_pct":  13.0, "pe":  24.0, "mkt_cap_t": 2.40, "range_52w": [150.0, 207.0]},
    {"ticker": "META",  "name": "Meta",           "theme": "Hyperscaler", "price": 612.0, "daily_pct":  0.0, "ytd_pct":   4.0, "pe":  28.0, "mkt_cap_t": 1.60, "range_52w": [480.0, 642.0]},
    {"ticker": "AMZN",  "name": "Amazon",         "theme": "Hyperscaler", "price": 224.0, "daily_pct":  0.4, "ytd_pct":   2.0, "pe":  41.0, "mkt_cap_t": 2.40, "range_52w": [165.0, 242.0]},
    {"ticker": "PLTR",  "name": "Palantir",       "theme": "Apps",        "price":  78.0, "daily_pct":  1.6, "ytd_pct":   3.0, "pe": 312.0, "mkt_cap_t": 0.19, "range_52w": [ 60.0,  98.0]},
    {"ticker": "ORCL",  "name": "Oracle",         "theme": "Hyperscaler", "price": 215.0, "daily_pct":  0.3, "ytd_pct":  29.0, "pe":  38.0, "mkt_cap_t": 0.60, "range_52w": [125.0, 230.0]},
]
AI_STOCKS_AS_OF = "2025-12-15"

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
