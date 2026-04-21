"""Offline watermark recovery — reverse the signer to identify a leaker.

Entry points:

    identify_leak(image_bytes=None, text=None, payload=None) -> dict

Call with whatever evidence you have. At least one of ``image_bytes`` /
``text`` / ``payload`` must be non-empty. The routine:

  1. If image bytes are provided, run pytesseract when available to
     extract any legible watermark text, and look for visible ``sid:XXXX``
     fragments. A direct hit identifies the leaker with confidence 1.0.

  2. If text is provided (e.g. from a chat log leak), search for sentinel
     fingerprints from ``sentinel_predictions`` and for the ``uid:``
     marker that the visible SVG carries.

  3. If ``payload`` (a list of dicts) is provided, run the numeric-payload
     scorer from ``signer.recover_seed_from_numeric_payload`` over every
     known user seed and return the top match.

All three paths return the same-shaped dict so the admin UI can render a
uniform report:

    {
      "source": "ocr" | "sentinel" | "numeric_payload" | None,
      "user_id": int | None,
      "email": str | None,
      "confidence": float in [0, 1],
      "evidence": [human-readable strings],
    }

Designed to be callable from a CLI one-off (``python -m forensics.extract_watermark``)
AND from the admin route at ``/admin/security/forensics``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import db
from forensics import signer as _signer


log = logging.getLogger(__name__)


# ── OCR path ─────────────────────────────────────────────────────────────

_SID_RE = re.compile(r"sid:([a-f0-9]{4,16})", re.IGNORECASE)
_UID_RE = re.compile(r"uid:(\d+)", re.IGNORECASE)


def _try_ocr(image_bytes: bytes) -> str:
    """Run pytesseract if installed; return extracted text or empty string."""
    try:
        import io
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore
    except ImportError:
        log.info("pytesseract/Pillow not installed — skipping OCR path")
        return ""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        return pytesseract.image_to_string(img) or ""
    except Exception as exc:
        log.warning("OCR failed: %s", exc)
        return ""


def _resolve_by_uid(uid: int) -> Optional[dict]:
    try:
        row = db.get_user_by_id(uid)
        return {"user_id": row["id"], "email": row["email"]} if row else None
    except Exception:
        return None


def _resolve_by_session_suffix(suffix: str) -> Optional[dict]:
    """Match a 4-16 char suffix against known session token hashes."""
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT user_id, session_id FROM watermark_seeds "
                "WHERE session_id LIKE ? "
                "LIMIT 2",
                (f"%{suffix}",),
            ).fetchall()
        if len(rows) == 1:
            user_id = rows[0]["user_id"]
            u = db.get_user_by_id(user_id)
            return {"user_id": user_id, "email": (u["email"] if u else None)}
    except Exception as exc:
        log.warning("session-suffix resolve failed: %s", exc)
    return None


def _ocr_path(image_bytes: bytes) -> Optional[dict]:
    text = _try_ocr(image_bytes)
    if not text:
        return None
    uid_m = _UID_RE.search(text)
    sid_m = _SID_RE.search(text)
    evidence = []
    if uid_m:
        uid = int(uid_m.group(1))
        hit = _resolve_by_uid(uid)
        if hit:
            evidence.append(f"OCR'd uid:{uid} from image watermark")
            return {
                "source": "ocr",
                "user_id": hit["user_id"],
                "email": hit["email"],
                "confidence": 1.0 if sid_m else 0.9,
                "evidence": evidence,
            }
    if sid_m:
        sid = sid_m.group(1).lower()
        hit = _resolve_by_session_suffix(sid)
        if hit:
            evidence.append(f"OCR'd session suffix {sid} from image watermark")
            return {
                "source": "ocr",
                "user_id": hit["user_id"],
                "email": hit["email"],
                "confidence": 0.85,
                "evidence": evidence,
            }
    return None


# ── Text / sentinel path ──────────────────────────────────────────────────

def _sentinel_path(text: str) -> Optional[dict]:
    """Search leaked text for any sentinel prediction ids we've issued."""
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT user_id, sentinel_id, endpoint, payload_json "
                "FROM sentinel_predictions "
                "ORDER BY injected_at DESC LIMIT 5000"
            ).fetchall()
    except Exception as exc:
        log.warning("sentinel_predictions read failed: %s", exc)
        return None

    lower = text.lower()
    for row in rows:
        sid = row["sentinel_id"].lower()
        if sid in lower:
            u = _resolve_by_uid(row["user_id"])
            return {
                "source": "sentinel",
                "user_id": row["user_id"],
                "email": (u["email"] if u else None),
                "confidence": 0.98,
                "evidence": [
                    f"Sentinel id {sid} issued for endpoint {row['endpoint']} appears in leak",
                ],
            }
        # Also match on the synthetic title (which is unique per sentinel).
        try:
            payload = json.loads(row["payload_json"])
            title = (payload.get("title") or "").lower()
            if title and title in lower:
                u = _resolve_by_uid(row["user_id"])
                return {
                    "source": "sentinel",
                    "user_id": row["user_id"],
                    "email": (u["email"] if u else None),
                    "confidence": 0.9,
                    "evidence": [
                        f"Sentinel title '{payload.get('title')}' (endpoint {row['endpoint']}) appears in leak",
                    ],
                }
        except Exception:
            continue
    return None


def _uid_in_text(text: str) -> Optional[dict]:
    uid_m = _UID_RE.search(text)
    if not uid_m:
        return None
    uid = int(uid_m.group(1))
    hit = _resolve_by_uid(uid)
    if hit:
        return {
            "source": "ocr",
            "user_id": hit["user_id"],
            "email": hit["email"],
            "confidence": 0.9,
            "evidence": [f"Visible watermark uid:{uid} present in leaked text"],
        }
    return None


# ── Numeric payload path ──────────────────────────────────────────────────

def _numeric_path(payload: list) -> Optional[dict]:
    """Walk every known user seed and return the best match."""
    try:
        with db.conn() as c:
            seed_rows = c.execute(
                "SELECT user_id, seed FROM user_forensic_seeds"
            ).fetchall()
    except Exception as exc:
        log.warning("user_forensic_seeds read failed: %s", exc)
        return None
    candidates = [(int(r["user_id"]), int(r["seed"])) for r in seed_rows]
    best = _signer.recover_seed_from_numeric_payload(payload, candidates)
    if not best:
        return None
    user_id, seed, score = best
    u = _resolve_by_uid(user_id)
    return {
        "source": "numeric_payload",
        "user_id": user_id,
        "email": (u["email"] if u else None),
        "confidence": round(score, 3),
        "evidence": [
            f"Decimal-precision watermark scored {score:.3f} against user {user_id}'s seed"
        ],
    }


# ── Public entry point ───────────────────────────────────────────────────

def identify_leak(
    *,
    image_bytes: Optional[bytes] = None,
    text: Optional[str] = None,
    payload: Optional[list] = None,
) -> dict:
    """Top-level recovery. Tries every available path; returns best hit."""
    results = []
    if image_bytes:
        hit = _ocr_path(image_bytes)
        if hit:
            results.append(hit)
    if text:
        hit = _sentinel_path(text)
        if hit:
            results.append(hit)
        hit2 = _uid_in_text(text)
        if hit2:
            results.append(hit2)
    if payload:
        hit = _numeric_path(payload)
        if hit:
            results.append(hit)

    if not results:
        return {
            "source": None,
            "user_id": None,
            "email": None,
            "confidence": 0.0,
            "evidence": ["No match across OCR / sentinel / numeric-payload paths."],
        }

    results.sort(key=lambda r: r["confidence"], reverse=True)
    top = results[0]
    # Merge evidence from other-source hits if they point at the same user.
    for other in results[1:]:
        if other["user_id"] == top["user_id"]:
            top["evidence"].extend(other["evidence"])
            top["confidence"] = min(1.0, top["confidence"] + 0.03)
    return top


def _cli() -> int:
    import argparse, sys
    ap = argparse.ArgumentParser(description="narve.ai forensic watermark recovery")
    ap.add_argument("--image", help="Path to a leaked screenshot")
    ap.add_argument("--text", help="Path to a leaked text dump")
    ap.add_argument("--payload", help="Path to a JSON list to match against numeric seeds")
    args = ap.parse_args()
    image_bytes = open(args.image, "rb").read() if args.image else None
    text = open(args.text, "r").read() if args.text else None
    payload = json.load(open(args.payload, "r")) if args.payload else None
    result = identify_leak(image_bytes=image_bytes, text=text, payload=payload)
    print(json.dumps(result, indent=2))
    return 0 if result["user_id"] is not None else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
