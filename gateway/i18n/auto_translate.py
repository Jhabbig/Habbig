"""One-off: use Claude Sonnet to pre-translate the English locale into ES,
DE, and PT-BR. The output is flagged ``_machine: true`` so a human
reviewer knows which entries to spot-check.

Run ad-hoc — it's not part of any pipeline. The script **never**
overwrites a value that is already a plain string (human-verified) in a
target locale, so partial manual reviews survive a re-run.

Usage::

    # Translate every missing / machine-flagged key into all three targets
    ANTHROPIC_API_KEY=sk-... python3 -m gateway.i18n.auto_translate

    # Only one language
    python3 -m gateway.i18n.auto_translate --langs es

    # Pretend-run — prints what would change without touching disk
    python3 -m gateway.i18n.auto_translate --dry-run

Design notes:
  * One API call per batch of ~30 keys. Keeps latency low and lets the
    model keep context across a screen of UI copy (helps tone).
  * Brand names and platform names are pinned in the system prompt so
    they don't get translated (narve.ai, Polymarket, Kalshi).
  * Placeholders like ``{count}`` / ``{edge}`` / ``{plural}`` are also
    pinned — the model must return them verbatim.
  * Exit code is 0 even when individual batches fail. Partial wins count;
    the retry is just re-running the script.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable


LOCALES_DIR = Path(__file__).parent / "locales"
TARGET_LANGS = ["es", "de", "pt-br"]

LANG_NAMES = {
    "es": "Spanish (European Spanish, neutral)",
    "de": "German (formal / professional register, du is fine)",
    "pt-br": "Brazilian Portuguese (Brazilian, not European)",
}

BRAND_PINS = ["narve.ai", "Polymarket", "Kalshi", "Intelligence"]

SYSTEM_PROMPT = """\
You translate UI copy for narve.ai, a prediction-market intelligence
platform for professional operators and traders. The audience is fluent,
the tone is terse and professional, and the surface is compact: treat
every string as a UI element that must fit where the English fits.

Rules:
  * Do NOT translate these brand / product names (keep them verbatim):
    {brands}
  * Preserve every ``{{placeholder}}`` token exactly — they get
    substituted at render time.
  * Preserve sentence-initial capitalisation if the source has it; keep
    sentence case otherwise. Never all-caps.
  * Avoid idioms — this product runs globally, readability beats
    flavour. Match the register of the English rather than making it
    warmer or more formal.
  * If a string has multiple reasonable translations, pick the one
    that's closest to the English word count.

Output: valid JSON. A flat object mapping the ORIGINAL key (unchanged)
to the translated string. Nothing else. No prose, no code fences.
""".format(brands=", ".join(BRAND_PINS))


BATCH_SIZE = 30


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"warning: {path}: {e}", file=sys.stderr)
        return {}


def is_machine_or_missing(entry) -> bool:
    """True if the locale entry is absent or flagged as machine-generated.

    We never touch a plain string — that marks it as human-reviewed.
    """
    if entry is None:
        return True
    if isinstance(entry, dict):
        return bool(entry.get("_machine"))
    return False


def batches(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def build_user_prompt(lang: str, keys_values: list[tuple[str, str]]) -> str:
    lang_name = LANG_NAMES[lang]
    payload = {k: v for k, v in keys_values}
    return (
        f"Translate the following UI strings to {lang_name}. Return a JSON "
        f"object keyed by the same keys with the translations as values.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def call_claude(system: str, user: str, model: str = "claude-sonnet-4-6") -> dict:
    """Invoke the Anthropic API. Raises on transport error; returns the
    parsed JSON object the model produced. Never retries — the caller is
    expected to wrap batch logic in a try/except."""
    try:
        import anthropic
    except ImportError:
        print("auto_translate: anthropic package not installed. "
              "pip install anthropic and retry.", file=sys.stderr)
        sys.exit(2)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("auto_translate: ANTHROPIC_API_KEY unset.", file=sys.stderr)
        sys.exit(2)

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    ).strip()
    # Strip accidental ```json fences if the model slipped.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def translate_language(
    src: dict,
    target_lang: str,
    *,
    dry_run: bool = False,
    model: str = "claude-sonnet-4-6",
) -> int:
    """Translate every machine / missing entry in the target locale. Returns
    the number of entries actually written."""
    target_path = LOCALES_DIR / f"{target_lang}.json"
    target = load_json(target_path)

    to_translate: list[tuple[str, str]] = []
    for key, src_value in src.items():
        # Source is always a plain string in en.json — dict entries would
        # have been emitted by a prior auto_translate pass accidentally.
        if isinstance(src_value, dict):
            src_value = src_value.get("text", "")
        if not isinstance(src_value, str) or not src_value:
            continue
        if is_machine_or_missing(target.get(key)):
            to_translate.append((key, src_value))

    if not to_translate:
        print(f"  {target_lang}: nothing to translate — all keys human-reviewed.")
        return 0

    print(f"  {target_lang}: {len(to_translate)} entries to translate")
    if dry_run:
        return 0

    written = 0
    for batch in batches(to_translate, BATCH_SIZE):
        user_prompt = build_user_prompt(target_lang, batch)
        try:
            translations = call_claude(SYSTEM_PROMPT, user_prompt, model=model)
        except Exception as e:
            print(f"  {target_lang}: batch failed: {e}", file=sys.stderr)
            continue
        for key, _src in batch:
            txt = translations.get(key)
            if not isinstance(txt, str):
                continue
            target[key] = {"text": txt, "_machine": True}
            written += 1
        # Polite pause between batches to keep per-minute rate under cap.
        time.sleep(0.25)

    target_path.write_text(
        json.dumps(target, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    print(f"  {target_lang}: wrote {written} entries to {target_path}")
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--langs", nargs="+", choices=TARGET_LANGS, default=TARGET_LANGS,
        help="Which target languages to update (default: all).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="List what would change without calling the API.",
    )
    ap.add_argument(
        "--model", default="claude-sonnet-4-6",
        help="Anthropic model id. Sonnet 4.6 is the default.",
    )
    args = ap.parse_args()

    en_path = LOCALES_DIR / "en.json"
    src = load_json(en_path)
    if not src:
        print(f"auto_translate: en.json is empty or missing at {en_path}",
              file=sys.stderr)
        return 1

    total = 0
    for lang in args.langs:
        print(f"→ {lang}")
        total += translate_language(src, lang, dry_run=args.dry_run, model=args.model)

    print(f"done. {total} entries written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
