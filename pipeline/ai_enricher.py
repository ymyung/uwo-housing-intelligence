"""
ai_enricher.py  —  Stage 2 of the UWO housing pipeline.

Reads the website_ready CSV from uwo_listing_enricher.py, then:
  1. Promotes *_rule fields → canonical fields (rule source, no AI needed)
  2. Screens each row: only calls AI for fields still missing after promotion
  3. Calls Ollama (single prompt, temperature=0) to fill remaining gaps
  4. Merges with evidence gating: hard fields blocked if AI returns null evidence
  5. Optionally runs a true consensus second call (separate request, temp=0.2)
     for hard fields — but ONLY for fields that were actually AI-requested
     (rules-resolved fields are immune from consensus comparison)
  6. Validates merged result: contradiction flags + review_score
  7. Exports enriched CSV and a review-queue CSV

Key fixes vs previous version:
  - Removed repeat_prompt_count / repeat_prompt_text: repeating the prompt
    inside one call was adding noise, not accuracy. 2x was better than 3x.
    Use --consensus for a true second independent call instead.
  - run_consensus_pass: now gates on build_field_request_map so rule-resolved
    fields (e.g. furnished_rule=True) never trigger disagreement flags.
  - Evidence-blocked string fields now write their sentinel ("unknown") instead
    of leaving null, so downstream filters can distinguish "unknown" from
    "not yet processed".
  - validate_row: furnished_true_but_description_says_unfurnished only fires
    when furnished_source == "ai", not when it came from an amenity token.
  - utilities_status_source="rule" from scraper is now recognised in
    promote_rule_fields so utilities_status is treated as resolved.

Usage:
  python ai_enricher.py <input_csv> --output-csv <output_csv>
  python ai_enricher.py <input_csv> --output-csv <output_csv> --consensus
"""
import argparse
import json
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from tqdm import tqdm

import pandas as pd
import requests

try:
    from pipeline.run_context import RunContext
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline.run_context import RunContext


# ─── Ollama ───────────────────────────────────────────────────────────────────

OLLAMA_URL     = "http://localhost:11434/api/generate"
MODEL_DEFAULT  = "qwen2.5:14b-instruct"
DEFAULT_SYSTEMIC_ERROR_RATIO = 0.9
DEFAULT_SYSTEMIC_COMMON_ERROR_RATIO = 0.8
DEFAULT_SYSTEMIC_ERROR_MIN_CALLS = 10

AI_FURNISHED_RE = re.compile(r"\b(?:fully\s+)?furnished\b|\bfurniture\s+incl", re.I)
AI_UNFURNISHED_RE = re.compile(
    r"\b(?:unfurnished|not\s+furnished)\b"
    r"|\bfurnishings?\s+(?:are\s+)?not\s+included\b",
    re.I,
)
AI_SUBLET_RE = re.compile(
    r"\b(?:sublet(?:ting)?|subleas(?:e|ing)|sub-?let(?:ting)?|sub-?leas(?:e|ing)"
    r"|take\s+over\s+(?:my\s+)?lease|lease\s+take[\s-]?over|lease\s+transfer)\b",
    re.I,
)
AI_NOT_SUBLET_RE = re.compile(
    r"\b(?:not\s+a\s+sublet|no\s+subletting|cannot\s+sublet|no\s+sublease)\b",
    re.I,
)
AI_GENDER_PATTERNS = {
    "female_only": re.compile(r"\b(?:female|females|women|girls)\s+only\b", re.I),
    "male_only": re.compile(r"\b(?:male|males|men|boys)\s+only\b", re.I),
    "female_preferred": re.compile(
        r"\b(?:female|females|women)\s+(?:preferred|preference)\b"
        r"|\bprefer(?:red|ence)?\s+(?:female|females|women)\b",
        re.I,
    ),
    "male_preferred": re.compile(
        r"\b(?:male|males|men)\s+(?:preferred|preference)\b"
        r"|\bprefer(?:red|ence)?\s+(?:male|males|men)\b",
        re.I,
    ),
    "any": re.compile(
        r"\b(?:any\s+gender|all\s+genders|no\s+gender\s+preference|co-?ed)\b",
        re.I,
    ),
}


class SystemicAIServiceError(RuntimeError):
    """AI failures indicate a service-wide problem rather than isolated rows."""

    def __init__(self, message: str, metrics: dict[str, Any]):
        super().__init__(message)
        self.metrics = metrics


def detect_systemic_ai_failure(
    frame: pd.DataFrame,
    *,
    error_ratio: float = DEFAULT_SYSTEMIC_ERROR_RATIO,
    common_error_ratio: float = DEFAULT_SYSTEMIC_COMMON_ERROR_RATIO,
    minimum_calls: int = DEFAULT_SYSTEMIC_ERROR_MIN_CALLS,
) -> Optional[str]:
    """Return a secret-safe root cause when AI failures are dataset-wide."""

    if "ai_error" not in frame.columns:
        return None
    if "ai_called" in frame.columns:
        called = frame["ai_called"].eq(True)
    elif "ai_skipped" in frame.columns:
        called = frame["ai_skipped"].eq(False)
    else:
        return None
    call_count = int(called.sum())
    if call_count < minimum_calls:
        return None
    errors = frame.loc[called, "ai_error"].dropna().astype(str).str.strip()
    errors = errors[errors.ne("")]
    error_count = len(errors)
    if not error_count or error_count / call_count < error_ratio:
        return None
    normalized = errors.str.casefold().map(
        lambda value: (
            "ollama_connection_failure"
            if any(token in value for token in ("connection", "refused", "timed out"))
            else "ollama_model_unavailable"
            if "model" in value and any(
                token in value for token in ("missing", "not found", "unavailable")
            )
            else "ollama_http_failure"
            if "http" in value
            else "other_repeated_ai_failure"
        )
    )
    category, common_count = normalized.value_counts().index[0], int(
        normalized.value_counts().iloc[0]
    )
    if common_count / error_count < common_error_ratio:
        return None
    return (
        "Systemic AI service failure: "
        f"{error_count}/{call_count} AI calls failed; "
        f"dominant_category={category}; repeated_errors={common_count}."
    )


# ─── Rule field → canonical field mapping ─────────────────────────────────────
#
# Fields with a *_rule suffix in the scraper output.
# Promotion happens before AI — AI is only called for fields still None after this.

RULE_FIELD_MAP: Dict[str, str] = {
    "parking_available_rule":  "parking_available",
    "parking_spaces_rule":     "parking_spaces",
    "bathrooms_rule":          "bathrooms",
    "bathroom_type_rule":      "bathroom_type",
    "air_conditioning_rule":   "air_conditioning",
    "laundry_rule":            "laundry",
    "dishwasher_rule":         "dishwasher",
    "furnished_rule":          "furnished",
    "lease_term_months_rule":  "lease_term_months",
    "lease_type_rule":         "lease_type",
    "utilities_included_rule": "utilities_included",
    "preferred_gender_rule":   "preferred_gender",
    "tenant_type_rule":        "tenant_type",
}

# Sentinel values that count as "unset" for enum/string fields
RULE_UNSET_SENTINELS = {None, "", "not_specified", "unknown"}

# Fallback sentinel written when evidence gate blocks a string field.
# Prevents null from being ambiguous ("null = not run" vs "null = blocked").
EVIDENCE_BLOCK_SENTINEL: Dict[str, Any] = {
    "lease_type":        "unknown",
    "utilities_status":  "unknown",
    "bathroom_type":     "unknown",
    "is_sublet":         None,    # boolean — leave null, unknown not valid
    "bathrooms":         None,    # numeric — leave null
    "lease_term_months": None,    # numeric — leave null
    "furnished":         None,    # boolean — leave null
    "preferred_gender":  "not_specified",
}


# ─── Evidence gating ─────────────────────────────────────────────────────────

EVIDENCE_REQUIRED_FIELDS = {
    "bathrooms",
    "bathroom_type",
    "lease_term_months",
    "lease_type",
    "utilities_included",
    "utilities_status",
    "furnished",
    "is_sublet",
    "preferred_gender",
}

EVIDENCE_KEY_MAP: Dict[str, str] = {
    "bathrooms":          "bathrooms",
    "bathroom_type":      "bathrooms",
    "lease_term_months":  "lease_term",
    "lease_type":         "lease_term",
    "utilities_included": "utilities",
    "utilities_status":   "utilities",
    "furnished":          "furnished",
    "is_sublet":          "sublet",
    "preferred_gender":   "preferred_gender",
}


# ─── Consensus hard fields ────────────────────────────────────────────────────
#
# When --consensus is on, these get a second call if first-pass confidence is
# low/medium — but ONLY when the field was actually AI-requested (not rule-resolved).

CONSENSUS_HARD_FIELDS = {
    "bathrooms",
    "bathroom_type",
    "lease_type",
    "lease_term_months",
    "utilities_included",
    "utilities_status",
    "is_sublet",
    "furnished",
}

# Maps a hard field to its request_map group key
CONSENSUS_FIELD_TO_GROUP: Dict[str, str] = {
    "bathrooms":          "bathrooms",
    "bathroom_type":      "bathrooms",
    "lease_type":         "lease",
    "lease_term_months":  "lease",
    "utilities_included": "utilities",
    "utilities_status":   "utilities",
    "is_sublet":          "is_sublet",
    "furnished":          "furnished",
}


# ─── JSON schema sent to Ollama ───────────────────────────────────────────────

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_sublet":           {"type": ["boolean", "null"]},
        "sublet_confidence":   {"type": "string", "enum": ["none", "low", "medium", "high"]},

        "bathrooms":           {"type": ["number", "null"]},
        "bathroom_type":       {"type": ["string", "null"], "enum": ["private", "shared", "unknown", None]},
        "bathroom_confidence": {"type": "string", "enum": ["none", "low", "medium", "high"]},

        "parking_available":   {"type": ["boolean", "null"]},
        "parking_spaces":      {"type": ["integer", "null"]},
        "parking_confidence":  {"type": "string", "enum": ["none", "low", "medium", "high"]},

        "laundry":             {"type": ["boolean", "null"]},
        "laundry_confidence":  {"type": "string", "enum": ["none", "low", "medium", "high"]},

        "furnished":           {"type": ["boolean", "null"]},
        "furnished_confidence":{"type": "string", "enum": ["none", "low", "medium", "high"]},

        "air_conditioning":             {"type": ["boolean", "null"]},
        "air_conditioning_confidence":  {"type": "string", "enum": ["none", "low", "medium", "high"]},

        "dishwasher":           {"type": ["boolean", "null"]},
        "dishwasher_confidence":{"type": "string", "enum": ["none", "low", "medium", "high"]},

        "tenant_type": {
            "type": "string",
            "enum": ["student", "professional", "family", "staff_faculty", "not_specified"],
        },
        "tenant_type_confidence": {"type": "string", "enum": ["none", "low", "medium", "high"]},

        "preferred_gender": {
            "type": "string",
            "enum": [
                "male_preferred", "female_preferred",
                "male_only", "female_only", "any", "not_specified",
            ],
        },
        "preferred_gender_confidence": {"type": "string", "enum": ["none", "low", "medium", "high"]},

        "lease_term_months":    {
            "type": ["integer", "null"], "minimum": 1, "maximum": 36
        },
        "lease_type": {
            "type": "string",
            "enum": ["sublet", "short_term", "fixed_term", "standard", "unknown"],
        },
        "lease_term_confidence":{"type": "string", "enum": ["none", "low", "medium", "high"]},

        "utilities_included":  {"type": ["boolean", "null"]},
        "utilities_status": {
            "type": "string",
            "enum": ["all_included", "partially_included", "not_included", "unknown"],
        },
        "utilities_confidence":{"type": "string", "enum": ["none", "low", "medium", "high"]},

        "evidence": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "sublet":           {"type": ["string", "null"]},
                "bathrooms":        {"type": ["string", "null"]},
                "parking":          {"type": ["string", "null"]},
                "laundry":          {"type": ["string", "null"]},
                "furnished":        {"type": ["string", "null"]},
                "air_conditioning": {"type": ["string", "null"]},
                "dishwasher":       {"type": ["string", "null"]},
                "tenant_type":      {"type": ["string", "null"]},
                "preferred_gender": {"type": ["string", "null"]},
                "lease_term":       {"type": ["string", "null"]},
                "utilities":        {"type": ["string", "null"]},
            },
            "required": [
                "sublet", "bathrooms", "parking", "laundry", "furnished",
                "air_conditioning", "dishwasher", "tenant_type", "preferred_gender",
                "lease_term", "utilities",
            ],
        },
    },
    "required": [
        "is_sublet", "sublet_confidence",
        "bathrooms", "bathroom_type", "bathroom_confidence",
        "parking_available", "parking_spaces", "parking_confidence",
        "laundry", "laundry_confidence",
        "furnished", "furnished_confidence",
        "air_conditioning", "air_conditioning_confidence",
        "dishwasher", "dishwasher_confidence",
        "tenant_type", "tenant_type_confidence",
        "preferred_gender", "preferred_gender_confidence",
        "lease_term_months", "lease_type", "lease_term_confidence",
        "utilities_included", "utilities_status", "utilities_confidence",
        "evidence",
    ],
}

SYSTEM_PROMPT = """
You extract structured housing attributes from university off-campus listing text.

PRIORITY RULES:
1. Explicit structured fields from the website (in existing_structured_fields) are
   AUTHORITATIVE. Do not override them.
2. Only fill fields whose group is explicitly marked true in request_map.
3. Be CONSERVATIVE. If the information is not clearly present in the text, return null
   or not_specified. Do not guess. Do not infer from silence.
4. Evidence MUST be a short verbatim snippet from the input text. If you cannot find
   supporting text, set evidence to null AND set the field value to null.
   Never return a non-null field value alongside null evidence.

FIELD-SPECIFIC RULES:
- utilities_included: True ONLY when ALL major utilities (electricity, heat, water)
  are confirmed included. If only some are included (gas only, internet only, etc.),
  set utilities_included=null and utilities_status="partially_included".
- utilities_status: "all_included" only for explicit all-inclusive claims.
  "partially_included" when specific named utilities are included but not all.
  "not_included" only when explicitly stated as extra or not included.
  "unknown" when no utility information is present.
- is_sublet: True only for explicit transfer language: "sublet", "sublease",
  "take over my lease", "lease takeover", "lease transfer". Not for vague phrasing.
- lease_type: must be "sublet" when is_sublet=True.
- bathrooms: only infer from description if explicitly stated (e.g. "2 full bathrooms").
  Return null if the count is ambiguous.
- bathroom_type: "private" only if explicitly stated for this tenant's specific unit or room.
- furnished: True for "Furniture Incl"/"Furnished Bdrm(s)" in amenities or "furnished"
  in description. False for "unfurnished". Null if no clear signal.
- preferred_gender: a structured "Preferred Gender: Male/Female" is a preference,
  never an exclusion. Use male_preferred/female_preferred for explicit preference
  wording. Use male_only/female_only only for separate explicit "only" wording.
- May-August or other summer availability does not imply a sublet.
- Return JSON only. No prose, no markdown fences.
""".strip()


# ─── Type helpers ─────────────────────────────────────────────────────────────

def safe_val(value: Any) -> Any:
    if pd.isna(value):
        return None
    return value


def norm_str(value: Any) -> Optional[str]:
    value = safe_val(value)
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def norm_bool(value: Any) -> Optional[bool]:
    value = safe_val(value)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:  return True
    if text in {"false", "0", "no"}: return False
    return None


def norm_int(value: Any) -> Optional[int]:
    value = safe_val(value)
    if value is None:
        return None
    try:
        if str(value).strip() == "":
            return None
        return int(float(value))
    except Exception:
        return None


def norm_float(value: Any) -> Optional[float]:
    value = safe_val(value)
    if value is None:
        return None
    try:
        if str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_evidence_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def evidence_is_verbatim(row: pd.Series, evidence: Any) -> bool:
    """Require AI evidence to occur in the captured website input."""
    needle = _normalized_evidence_text(evidence)
    if not needle:
        return False
    source_fields = (
        "title",
        "description",
        "amenities",
        "amenities_list",
        "lease_term_raw",
        "preferred_gender_raw",
        "utilities_raw",
        "housing_type_raw",
        "date_available_raw",
    )
    return any(
        needle in _normalized_evidence_text(safe_val(row.get(field_name)))
        for field_name in source_fields
    )


def ai_value_has_semantic_support(
    target: str, ai_value: Any, evidence: Any
) -> bool:
    """Reject deterministic contradictions even when an evidence string exists."""
    text = str(evidence or "")
    if target == "furnished":
        value = norm_bool(ai_value)
        if value is True:
            return bool(AI_FURNISHED_RE.search(text))
        if value is False:
            return bool(AI_UNFURNISHED_RE.search(text))
    if target == "is_sublet":
        value = norm_bool(ai_value)
        if value is True:
            return bool(AI_SUBLET_RE.search(text))
        if value is False:
            return bool(AI_NOT_SUBLET_RE.search(text))
    if target == "preferred_gender":
        normalized = norm_str(ai_value)
        if normalized in {None, "not_specified"}:
            return True
        pattern = AI_GENDER_PATTERNS.get(normalized)
        return bool(pattern and pattern.search(text))
    if target == "bathroom_type":
        normalized = norm_str(ai_value)
        if normalized == "private":
            return bool(re.search(r"\b(?:private|own|ensuite|en-suite)\b", text, re.I))
        if normalized == "shared":
            return bool(re.search(r"\bshared?\b|\bshare\b", text, re.I))
    if target == "lease_type":
        normalized = norm_str(ai_value)
        if normalized == "sublet":
            return bool(AI_SUBLET_RE.search(text))
        if normalized == "standard":
            return bool(re.search(r"\b12(?:\.0)?\s*-?\s*months?\b", text, re.I))
        if normalized == "short_term":
            return bool(
                re.search(r"\bmonth[\s-]to[\s-]month\b|\bm2m\b", text, re.I)
                or re.search(r"\b[1-7](?:\.0)?\s*-?\s*months?\b", text, re.I)
            )
        if normalized == "fixed_term":
            return bool(
                re.search(r"\bfixed[\s-]term\b", text, re.I)
                or re.search(r"\b(?:8|9|10)(?:\.0)?\s*-?\s*months?\b", text, re.I)
            )
    return True


# ─── Rule promotion ───────────────────────────────────────────────────────────

def promote_rule_fields(row: pd.Series) -> Dict[str, Any]:
    """
    Promote *_rule fields → canonical fields and record source="rule".
    Also recognises utilities_status when utilities_status_source="rule" in the row
    (written by the scraper) so the enricher skips AI for that field.

    Returns a dict of {canonical_field: value, canonical_field_source: "rule"}.
    """
    promoted: Dict[str, Any] = {}

    for rule_col, canonical in RULE_FIELD_MAP.items():
        rule_val = safe_val(row.get(rule_col))
        if rule_val is None:
            continue

        if canonical in {"parking_available", "air_conditioning", "laundry",
                         "dishwasher", "furnished", "utilities_included"}:
            normalized = norm_bool(rule_val)
        elif canonical in {"parking_spaces", "lease_term_months"}:
            normalized = norm_int(rule_val)
        elif canonical == "bathrooms":
            normalized = norm_float(rule_val)
        else:
            normalized = norm_str(rule_val)

        if normalized in RULE_UNSET_SENTINELS:
            continue

        # Don't overwrite an existing non-sentinel canonical value
        existing = safe_val(row.get(canonical))
        if existing is not None and norm_str(existing) not in RULE_UNSET_SENTINELS:
            continue

        promoted[canonical]             = normalized
        promoted[f"{canonical}_source"] = "rule"

    # ── Special: utilities_status already set by scraper (no _rule suffix) ──
    # If utilities_status_source="rule" is present in the row, record it in
    # promoted so consensus and validation logic respect the rule provenance.
    us_source = norm_str(safe_val(row.get("utilities_status_source")))
    us_val    = norm_str(safe_val(row.get("utilities_status")))
    if us_source == "rule" and us_val and us_val not in RULE_UNSET_SENTINELS:
        # utilities_status is already in the row; just mark its source
        if "utilities_status_source" not in promoted:
            promoted["utilities_status_source"] = "rule"

    return promoted


# ─── AI gating ────────────────────────────────────────────────────────────────

def needs_ai_for_field(row: pd.Series, field_name: str, promoted: Dict[str, Any]) -> bool:
    """Returns True if the field is still unset after rule promotion."""
    if field_name in promoted:
        val = promoted[field_name]
    else:
        val = safe_val(row.get(field_name))

    if val is None:
        return True
    if isinstance(val, str) and val.strip() in RULE_UNSET_SENTINELS:
        return True
    return False


def build_field_request_map(row: pd.Series, promoted: Dict[str, Any]) -> Dict[str, bool]:
    needs = lambda f: needs_ai_for_field(row, f, promoted)
    return {
        "is_sublet":        needs("is_sublet"),
        "bathrooms":        needs("bathrooms"),
        "parking":          needs("parking_available") or needs("parking_spaces"),
        "laundry":          needs("laundry"),
        "furnished":        needs("furnished"),
        "air_conditioning": needs("air_conditioning"),
        "dishwasher":       needs("dishwasher"),
        "tenant_type":      needs("tenant_type"),
        "preferred_gender": needs("preferred_gender"),
        "lease":            needs("lease_type") or needs("lease_term_months"),
        "utilities":        needs("utilities_included") or needs("utilities_status"),
    }


def should_enrich(row: pd.Series, promoted: Dict[str, Any]) -> bool:
    has_text = any(
        isinstance(x, str) and x.strip()
        for x in [row.get("description"), row.get("amenities"),
                  row.get("lease_term_raw"), row.get("title")]
    )
    if not has_text:
        return False
    return any(build_field_request_map(row, promoted).values())


# ─── Prompt builder ───────────────────────────────────────────────────────────

def build_prompt(row: pd.Series, promoted: Dict[str, Any]) -> str:
    request_map = build_field_request_map(row, promoted)

    existing = {
        "listing_id":       safe_val(row.get("listing_id")),
        "title":            safe_val(row.get("title")),
        "address":          safe_val(row.get("address")),
        "price_text":       safe_val(row.get("price_text")),
        "housing_type":     safe_val(row.get("housing_type")),
        "bedrooms":         safe_val(row.get("bedrooms")),
        "date_available":   safe_val(row.get("date_available")),
        "lease_term_raw":   safe_val(row.get("lease_term_raw")),
        "preferred_gender": promoted.get("preferred_gender") or safe_val(row.get("preferred_gender")),
        "tenant_type":      promoted.get("tenant_type")      or safe_val(row.get("tenant_type")),
        "utilities_included": (
            promoted.get("utilities_included") if "utilities_included" in promoted
            else safe_val(row.get("utilities_included_rule"))
        ),
        "utilities_status": safe_val(row.get("utilities_status")),
        "bathrooms":        promoted.get("bathrooms")       or safe_val(row.get("bathrooms")),
        "bathroom_type":    promoted.get("bathroom_type")   or safe_val(row.get("bathroom_type_rule")),
        "parking_available": (
            promoted.get("parking_available") if "parking_available" in promoted
            else safe_val(row.get("parking_available_rule"))
        ),
        "parking_spaces": (
            promoted.get("parking_spaces") if "parking_spaces" in promoted
            else safe_val(row.get("parking_spaces_rule"))
        ),
        "laundry":          promoted.get("laundry")          or safe_val(row.get("laundry_rule")),
        "furnished": (
            promoted.get("furnished") if "furnished" in promoted
            else safe_val(row.get("furnished_rule"))
        ),
        "air_conditioning": promoted.get("air_conditioning") or safe_val(row.get("air_conditioning_rule")),
        "dishwasher":       promoted.get("dishwasher")       or safe_val(row.get("dishwasher_rule")),
        "is_sublet":        promoted.get("is_sublet")        or safe_val(row.get("is_sublet")),
        "lease_term_months": (
            promoted.get("lease_term_months") or safe_val(row.get("lease_term_months_rule"))
        ),
        "lease_type": promoted.get("lease_type") or safe_val(row.get("lease_type_rule")),
    }

    payload = {
        "instructions": {
            "only_fill_fields_marked_true_in_request_map": True,
            "do_not_override_existing_structured_fields":  True,
            "if_evidence_is_null_set_field_to_null":       True,
        },
        "request_map":              request_map,
        "existing_structured_fields": existing,
        "free_text_sources": {
            "amenities":   safe_val(row.get("amenities")),
            "description": safe_val(row.get("description")),
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ─── Ollama call ──────────────────────────────────────────────────────────────

def call_ollama(model: str, prompt: str, temperature: float = 0.0) -> Dict[str, Any]:
    payload = {
        "model":   model,
        "prompt":  prompt,
        "system":  SYSTEM_PROMPT,
        "format":  SCHEMA,
        "stream":  False,
        "options": {"temperature": temperature},
    }
    response = requests.post(OLLAMA_URL, json=payload, timeout=180)
    response.raise_for_status()
    data     = response.json()
    raw_text = data.get("response", "").strip()
    if not raw_text:
        raise ValueError("Ollama returned an empty response.")
    return json.loads(raw_text)


def flatten_enrichment(data: Dict[str, Any]) -> Dict[str, Any]:
    evidence = data.pop("evidence", {}) or {}
    flat     = {f"ai_{k}": v for k, v in data.items()}
    for k, v in evidence.items():
        flat[f"ai_evidence_{k}"] = v
    return flat


# ─── Consensus second pass ────────────────────────────────────────────────────

def run_consensus_pass(
    row:          pd.Series,
    promoted:     Dict[str, Any],
    model:        str,
    first_result: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """
    For hard fields where first-pass confidence is low/medium AND the field was
    actually AI-requested (not rule-resolved), run a second independent call at
    temperature=0.2 and compare.

    Fields where rule promotion already set a value are SKIPPED — they are
    immune from consensus comparison. This is the critical fix that prevents
    the 13/24 false 'consensus_disagreement_furnished' flags seen in the trial.

    Agreement  → keep first result.
    Disagreement → null the value, add flag for manual review.
    """
    request_map = build_field_request_map(row, promoted)

    conf_lookup = {
        "bathrooms":          "ai_bathroom_confidence",
        "bathroom_type":      "ai_bathroom_confidence",
        "lease_type":         "ai_lease_term_confidence",
        "lease_term_months":  "ai_lease_term_confidence",
        "utilities_included": "ai_utilities_confidence",
        "utilities_status":   "ai_utilities_confidence",
        "is_sublet":          "ai_sublet_confidence",
        "furnished":          "ai_furnished_confidence",
    }

    uncertain_fields = set()
    for field in CONSENSUS_HARD_FIELDS:
        group = CONSENSUS_FIELD_TO_GROUP.get(field, field)

        # Skip if rule already resolved this field's group
        if not request_map.get(group, False):
            continue

        ck = conf_lookup.get(field)
        if ck and first_result.get(ck) in {"low", "medium", "none"}:
            uncertain_fields.add(field)

    if not uncertain_fields:
        return first_result, []

    try:
        second_raw    = call_ollama(model=model, prompt=build_prompt(row, promoted), temperature=0.2)
        second_result = flatten_enrichment(second_raw)
    except Exception:
        return first_result, []

    disagreements: List[str] = []
    merged = dict(first_result)

    for field in uncertain_fields:
        ai_key = f"ai_{field}"
        v1     = first_result.get(ai_key)
        v2     = second_result.get(ai_key)
        if v1 == v2:
            continue
        merged[ai_key] = None
        disagreements.append(f"consensus_disagreement_{field}")

    return merged, disagreements


# ─── Merge ────────────────────────────────────────────────────────────────────

def merge_ai_row(
    base_row: pd.Series,
    promoted: Dict[str, Any],
    ai_row:   Dict[str, Any],
) -> Dict[str, Any]:
    """
    Merge priority: website explicit → rule-promoted → AI (evidence-gated).
    Evidence-blocked string fields write their sentinel ("unknown") instead of null.
    """
    out = base_row.to_dict()

    # Store raw AI outputs for auditability
    for k, v in ai_row.items():
        out[k] = v

    # Apply rule-promoted values
    for k, v in promoted.items():
        out[k] = v

    field_map = {
        "ai_is_sublet":          "is_sublet",
        "ai_bathrooms":          "bathrooms",
        "ai_bathroom_type":      "bathroom_type",
        "ai_parking_available":  "parking_available",
        "ai_parking_spaces":     "parking_spaces",
        "ai_laundry":            "laundry",
        "ai_furnished":          "furnished",
        "ai_air_conditioning":   "air_conditioning",
        "ai_dishwasher":         "dishwasher",
        "ai_tenant_type":        "tenant_type",
        "ai_preferred_gender":   "preferred_gender",
        "ai_lease_term_months":  "lease_term_months",
        "ai_lease_type":         "lease_type",
        "ai_utilities_included": "utilities_included",
        "ai_utilities_status":   "utilities_status",
    }

    for ai_col, target in field_map.items():
        if ai_col not in ai_row:
            continue

        ai_value = ai_row.get(ai_col)

        # A zero-month lease is an old invalid inference, not a real lease term.
        # Keep it unknown even when legacy cached evidence happens to contain 0.
        if target == "lease_term_months" and norm_int(ai_value) == 0:
            out["lease_term_months_ai_evidence_blocked"] = True
            continue

        # ── 1. Hard-protect explicit website structured values ─────────────
        if target in {"preferred_gender", "tenant_type"}:
            current = norm_str(out.get(f"{target}_rule") or out.get(target))
            if current not in RULE_UNSET_SENTINELS:
                continue

        if target == "utilities_included":
            rule_val = norm_bool(out.get("utilities_included_rule"))
            if rule_val is not None:
                continue

        # ── 2. Rule-promoted fields are immune from AI override ────────────
        if out.get(f"{target}_source") == "rule":
            continue

        # Special: utilities_status set by scraper (utilities_status_source="rule")
        if target == "utilities_status":
            if norm_str(out.get("utilities_status_source")) == "rule":
                continue

        # ── 3. Evidence gate ───────────────────────────────────────────────
        if target in EVIDENCE_REQUIRED_FIELDS:
            ev_key   = EVIDENCE_KEY_MAP.get(target, target)
            evidence = ai_row.get(f"ai_evidence_{ev_key}")
            has_evidence = (
                evidence_is_verbatim(base_row, evidence)
                and ai_value_has_semantic_support(target, ai_value, evidence)
            )

            if not has_evidence:
                out[f"{target}_ai_evidence_blocked"] = True
                # Write sentinel for string fields so null is unambiguous
                sentinel = EVIDENCE_BLOCK_SENTINEL.get(target)
                current_val = safe_val(out.get(target))
                if current_val is None and sentinel is not None:
                    out[target]             = sentinel
                    out[f"{target}_source"] = "evidence_blocked_sentinel"
                continue

        # ── 4. Only fill currently-unset fields ───────────────────────────
        if ai_value is None:
            continue

        current_val = safe_val(out.get(target))
        is_unset    = current_val is None or (
            isinstance(current_val, str) and current_val.strip() in RULE_UNSET_SENTINELS
        )

        if is_unset:
            out[target]             = ai_value
            out[f"{target}_source"] = "ai"

    # ── Derive utilities_included from utilities_status if still unset ──────
    if safe_val(out.get("utilities_included")) is None:
        status = norm_str(out.get("utilities_status"))
        if status == "all_included":
            out["utilities_included"]        = True
            out["utilities_included_source"] = out.get("utilities_status_source", "derived")
        elif status == "not_included":
            out["utilities_included"]        = False
            out["utilities_included_source"] = out.get("utilities_status_source", "derived")

    # ── Enforce: is_sublet=True requires lease_type="sublet" ────────────────
    if out.get("is_sublet") is True:
        current_lt = norm_str(out.get("lease_type"))
        if current_lt not in {"sublet", None, "unknown"}:
            out["lease_type"]              = "sublet"
            out["lease_type_corrected_from"] = current_lt
            out["lease_type_source"]       = "forced_by_is_sublet"

    return out


# ─── Validation ───────────────────────────────────────────────────────────────

def validate_row(row: Dict[str, Any]) -> Tuple[List[str], int]:
    """
    Check for contradictions and weak-evidence issues.
    Returns (flags, review_score).  review_score = len(flags).
    """
    flags: List[str] = []

    is_sublet          = row.get("is_sublet")
    lease_type         = norm_str(row.get("lease_type"))
    utilities_included = norm_bool(row.get("utilities_included"))
    utilities_status   = norm_str(row.get("utilities_status"))
    furnished          = norm_bool(row.get("furnished"))
    furnished_source   = norm_str(row.get("furnished_source"))
    bathroom_type      = norm_str(row.get("bathroom_type"))
    parking_available  = norm_bool(row.get("parking_available"))
    description        = norm_str(row.get("description")) or ""
    amenities          = norm_str(row.get("amenities"))   or ""
    desc_lower         = description.lower()
    amenities_lower    = amenities.lower()

    # ── Sublet / lease_type ──────────────────────────────────────────────────
    if is_sublet is True and lease_type == "standard":
        flags.append("sublet_but_lease_type_standard")

    if is_sublet is False and lease_type == "sublet":
        flags.append("not_sublet_but_lease_type_sublet")

    # Structured availability evidence wins, but contradictory deterministic
    # text remains visible to the review queue.
    if norm_bool(row.get("availability_category_conflict")) is True:
        flags.append("availability_category_evidence_conflict")

    # ── Utilities ─────────────────────────────────────────────────────────────
    if utilities_included is True and utilities_status == "partially_included":
        flags.append("utilities_included_true_but_status_partial")

    if utilities_included is True and utilities_status == "not_included":
        flags.append("utilities_included_true_but_status_not_included")

    if utilities_included is True and any(
        x in desc_lower for x in ["utilities not included", "plus utilities", "utilities extra"]
    ):
        flags.append("utilities_included_contradicts_description")

    # ── Furnished ─────────────────────────────────────────────────────────────
    # Only flag if value came from AI — amenity tokens override description text
    if furnished is True and "unfurnished" in desc_lower and furnished_source == "ai":
        flags.append("furnished_true_but_description_says_unfurnished")

    positive_furnishing_text = AI_UNFURNISHED_RE.sub("", description)
    if furnished is False and AI_FURNISHED_RE.search(positive_furnishing_text):
        flags.append("furnished_false_but_description_mentions_furnished")

    # ── Bathroom ──────────────────────────────────────────────────────────────
    ai_bathroom_conf     = norm_str(row.get("ai_bathroom_confidence"))
    ai_bathroom_evidence = norm_str(row.get("ai_evidence_bathrooms"))
    if bathroom_type and bathroom_type not in {"unknown", None}:
        if ai_bathroom_evidence is None and row.get("bathroom_type_source") == "ai":
            flags.append("bathroom_type_from_ai_with_no_evidence")
        if ai_bathroom_conf in {"low", "none"} and row.get("bathroom_type_source") == "ai":
            flags.append("bathroom_type_low_ai_confidence")

    # ── Parking ───────────────────────────────────────────────────────────────
    if parking_available is False and "parking" in amenities_lower:
        flags.append("parking_available_false_but_parking_in_amenities")

    # ── Sublet AI confidence ──────────────────────────────────────────────────
    if is_sublet is True and row.get("is_sublet_source") == "ai":
        if norm_str(row.get("ai_sublet_confidence")) in {"low", "none"}:
            flags.append("is_sublet_true_but_ai_confidence_low")

    # ── Lease term range ──────────────────────────────────────────────────────
    lease_months = norm_int(row.get("lease_term_months"))
    if lease_months is not None and not (1 <= lease_months <= 24):
        flags.append(f"unusual_lease_term_months_{lease_months}")

    # ── Price range ───────────────────────────────────────────────────────────
    price = safe_val(row.get("price_monthly"))
    if price is None:
        price = safe_val(row.get("price_numeric"))
    if price is not None:
        try:
            pf = float(price)
            if pf > 3500 or pf < 250:
                flags.append(f"unusual_price_{int(pf)}")
        except (TypeError, ValueError):
            pass

    # ── Consensus disagreements (written by run_consensus_pass) ──────────────
    for k, v in row.items():
        if isinstance(k, str) and k.startswith("consensus_disagreement_") and v:
            flags.append(k)
        if isinstance(k, str) and k.endswith("_ai_evidence_blocked") and norm_bool(v):
            flags.append(k)

    return flags, len(flags)


# ─── Main enrichment loop ─────────────────────────────────────────────────────

def enrich_csv(
    input_csv:     Path,
    output_csv:    Path,
    model:         str,
    limit:         Optional[int]  = None,
    sleep_seconds: float          = 0.1,
    consensus:     bool           = False,
    review_queue_csv: Optional[Path] = None,
) -> pd.DataFrame:
    df = pd.read_csv(input_csv)
    if limit is not None:
        df = df.head(limit).copy()
    else:
        df = df.copy()

    merged_rows: List[Dict[str, Any]] = []
    total    = len(df)
    progress = tqdm(df.iterrows(), total=total, desc="Enriching")

    for idx, row in progress:
        listing_id = row.get("listing_id")
        progress.set_postfix_str(f"id={listing_id}")

        # ── Stage 1: Promote rule fields ──────────────────────────────────
        promoted = promote_rule_fields(row)

        # ── Stage 2: Skip if nothing needs AI ─────────────────────────────
        if not should_enrich(row, promoted):
            base = row.to_dict()
            base.update(promoted)
            base["ai_skipped"]    = True
            base["ai_skip_reason"] = "all_fields_resolved_by_rules"
            flags, score = validate_row(base)
            base["review_flags"]       = json.dumps(flags)
            base["review_score"]       = score
            base["needs_manual_review"] = score >= 1
            merged_rows.append(base)
            continue

        # ── Stage 3: AI call ──────────────────────────────────────────────
        try:
            prompt      = build_prompt(row, promoted)
            result_raw  = call_ollama(model=model, prompt=prompt, temperature=0.0)
            ai_flat     = flatten_enrichment(result_raw)

            # ── Stage 4 (optional): Consensus second call ──────────────────
            consensus_flags: List[str] = []
            if consensus:
                ai_flat, consensus_flags = run_consensus_pass(
                    row=row, promoted=promoted, model=model, first_result=ai_flat
                )

            # ── Stage 5: Merge ────────────────────────────────────────────
            merged = merge_ai_row(row, promoted, ai_flat)
            merged["ai_skipped"]    = False
            merged["ai_skip_reason"] = None

            for flag in consensus_flags:
                merged[flag] = True

        except Exception as exc:
            merged = row.to_dict()
            merged.update(promoted)
            merged["ai_error"]    = f"{type(exc).__name__}: {exc}"
            merged["ai_skipped"]  = False
            merged["ai_skip_reason"] = None

        # ── Stage 6: Validate ─────────────────────────────────────────────
        flags, score = validate_row(merged)
        merged["review_flags"]       = json.dumps(flags)
        merged["review_score"]       = score
        merged["needs_manual_review"] = score >= 1

        merged_rows.append(merged)

        if idx + 1 < total:
            time.sleep(sleep_seconds)

    out_df = pd.DataFrame(merged_rows)
    out_df.to_csv(output_csv, index=False)
    print(f"\nSaved enriched CSV → {output_csv}  ({len(out_df)} rows)")

    # ── Export review queue ───────────────────────────────────────────────────
    review_df = out_df[out_df["needs_manual_review"] == True].copy()
    if not review_df.empty:
        review_path = review_queue_csv or output_csv.with_name(
            output_csv.stem + "_review_queue.csv"
        )
        review_df   = review_df.sort_values("review_score", ascending=False)
        key_cols = [
            "listing_id", "listing_url", "title", "address",
            "review_score", "review_flags", "needs_manual_review",
            "is_sublet", "is_sublet_source",
            "availability_category", "availability_category_source",
            "availability_category_evidence", "availability_category_conflict",
            "lease_type", "lease_type_source", "lease_term_months",
            "bathroom_type", "bathroom_type_source", "bathrooms",
            "utilities_included", "utilities_status",
            "utilities_included_source", "utilities_status_source",
            "furnished", "furnished_source",
            "parking_available", "parking_spaces",
            "ai_evidence_sublet", "ai_evidence_bathrooms",
            "ai_evidence_lease_term", "ai_evidence_utilities", "ai_evidence_furnished",
            "description", "amenities",
        ]
        present = [c for c in key_cols if c in review_df.columns]
        review_df[present].to_csv(review_path, index=False)
        print(f"Review queue ({len(review_df)} rows, sorted by score) → {review_path}")
    else:
        review_path = review_queue_csv or output_csv.with_name(
            output_csv.stem + "_review_queue.csv"
        )
        review_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(columns=["listing_id", "review_score", "review_flags"]).to_csv(
            review_path, index=False
        )
        print("No rows flagged for manual review.")

    n_called  = out_df["ai_skipped"].eq(False).sum() if "ai_skipped" in out_df.columns else "?"
    n_skipped = out_df["ai_skipped"].eq(True).sum()  if "ai_skipped" in out_df.columns else "?"
    print(f"AI called: {n_called}  |  Skipped (rules resolved): {n_skipped}  |  Flagged for review: {len(review_df)}")

    return out_df


# ─── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "AI-enrich UWO housing listings via Ollama.\n"
            "Rule promotion → evidence-gated AI → optional consensus → validation."
        )
    )
    parser.add_argument("input_csv", nargs="?", type=Path, help="website_ready CSV from uwo_listing_enricher.py")
    parser.add_argument("--output-csv", type=Path)
    parser.add_argument("--review-queue-csv", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip", action="store_true", help="Explicitly skip AI in run mode.")
    parser.add_argument("--model",        type=str,  default=MODEL_DEFAULT)
    parser.add_argument("--limit",        type=int,  default=None, help="Only process first N rows")
    parser.add_argument("--sleep-seconds",type=float,default=0.1)
    parser.add_argument(
        "--consensus",
        action="store_true",
        default=False,
        help=(
            "Run a true second independent AI call (temperature=0.2) for hard fields "
            "when first-pass confidence is low/medium. Only fires for fields that were "
            "actually AI-requested (rule-resolved fields are immune). "
            "Disagreements are nulled and flagged for review. Doubles latency for "
            "uncertain rows — enable when accuracy > speed."
        ),
    )
    parser.add_argument(
        "--systemic-error-ratio",
        type=float,
        default=DEFAULT_SYSTEMIC_ERROR_RATIO,
    )
    parser.add_argument(
        "--systemic-common-error-ratio",
        type=float,
        default=DEFAULT_SYSTEMIC_COMMON_ERROR_RATIO,
    )
    parser.add_argument(
        "--systemic-error-min-calls",
        type=int,
        default=DEFAULT_SYSTEMIC_ERROR_MIN_CALLS,
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.resume and args.overwrite:
        parser.error("--resume and --overwrite are mutually exclusive")
    if (args.resume or args.overwrite) and args.run_dir is None:
        parser.error("--resume and --overwrite require --run-dir")
    if not 0 < args.systemic_error_ratio <= 1:
        parser.error("--systemic-error-ratio must be in (0, 1]")
    if not 0 < args.systemic_common_error_ratio <= 1:
        parser.error("--systemic-common-error-ratio must be in (0, 1]")
    if args.systemic_error_min_calls < 1:
        parser.error("--systemic-error-min-calls must be at least 1")

    context = None
    if args.run_dir is not None:
        if args.input_csv is not None or args.output_csv is not None or args.review_queue_csv is not None:
            parser.error("Explicit input/output paths cannot be combined with --run-dir")
        configuration = {
            "model": args.model,
            "consensus": args.consensus,
            "limit": args.limit,
            "sleep_seconds": args.sleep_seconds,
            "evidence_required_fields": sorted(EVIDENCE_REQUIRED_FIELDS),
            "consensus_hard_fields": sorted(CONSENSUS_HARD_FIELDS),
            "first_pass_temperature": 0.0,
            "consensus_temperature": 0.2,
            "systemic_error_ratio": args.systemic_error_ratio,
            "systemic_common_error_ratio": args.systemic_common_error_ratio,
            "systemic_error_min_calls": args.systemic_error_min_calls,
        }
        context = RunContext.open_for_cli(
            args.run_dir,
            resume=args.resume,
            overwrite=args.overwrite,
            command=sys.argv,
            configuration={"stage2": configuration},
        )
        input_csv = context.paths.stage1_website_ready
        output_csv = context.paths.stage2_enriched
        review_queue_csv = context.paths.stage2_review_queue
        outputs = [output_csv, review_queue_csv]
        context.ensure_outputs_available(outputs, allow_existing=args.resume or args.overwrite)
        if not input_csv.exists():
            error = FileNotFoundError(
                f"Stage 1 website-ready output not found: {input_csv}"
            )
            context.start_stage("stage2", input_paths=[input_csv], output_paths=outputs)
            context.fail_stage("stage2", error)
            raise error
        context.manifest["configuration"]["stage2"] = configuration
    else:
        if args.skip:
            parser.error("--skip requires --run-dir")
        if args.input_csv is None:
            parser.error("input_csv is required unless --run-dir is used")
        input_csv = args.input_csv
        output_csv = args.output_csv or Path("uwo_listing_details_ollama_ai.csv")
        review_queue_csv = args.review_queue_csv

    metrics: dict[str, Any] = {}
    try:
        input_rows = len(pd.read_csv(input_csv))
        if args.limit is not None:
            input_rows = min(input_rows, args.limit)
    except Exception as exc:
        if context is not None:
            context.start_stage(
                "stage2", input_paths=[input_csv], output_paths=outputs
            )
            context.fail_stage("stage2", exc)
        raise

    if args.skip:
        try:
            if args.limit is None:
                shutil.copyfile(input_csv, output_csv)
            else:
                pd.read_csv(input_csv).head(args.limit).to_csv(output_csv, index=False)
            pd.DataFrame(
                columns=["listing_id", "review_score", "review_flags"]
            ).to_csv(review_queue_csv, index=False)
            context.skip_stage(
                "stage2",
                reason="AI enrichment explicitly disabled.",
                input_paths=[input_csv],
                output_paths=[output_csv, review_queue_csv],
                input_rows=input_rows,
                output_rows=input_rows,
            )
        except Exception as exc:
            context.start_stage(
                "stage2", input_paths=[input_csv], output_paths=outputs
            )
            context.fail_stage("stage2", exc)
            raise
        return

    if context is not None:
        context.start_stage("stage2", input_paths=[input_csv], output_paths=outputs)

    try:
        out_df = enrich_csv(
            input_csv=input_csv,
            output_csv=output_csv,
            model=args.model,
            limit=args.limit,
            sleep_seconds=args.sleep_seconds,
            consensus=args.consensus,
            review_queue_csv=review_queue_csv,
        )
        review_count = int(
            out_df.get("needs_manual_review", pd.Series(dtype=bool)).eq(True).sum()
        )
        ai_error_count = int(
            out_df.get("ai_error", pd.Series(dtype=object)).notna().sum()
        )
        ai_call_count = int(
            out_df.get("ai_skipped", pd.Series(dtype=bool)).eq(False).sum()
        )
        metrics = {
            "review_count": review_count,
            "ai_call_count": ai_call_count,
            "ai_error_count": ai_error_count,
            "output_rows": len(out_df),
        }
        systemic_failure = detect_systemic_ai_failure(
            out_df,
            error_ratio=args.systemic_error_ratio,
            common_error_ratio=args.systemic_common_error_ratio,
            minimum_calls=args.systemic_error_min_calls,
        )
        if systemic_failure:
            raise SystemicAIServiceError(systemic_failure, metrics)
        if context is not None:
            warnings = []
            if review_count:
                warnings.append(f"{review_count} row(s) require manual review.")
            if ai_error_count:
                warnings.append(f"{ai_error_count} row(s) contain AI errors.")
            context.finish_stage(
                "stage2",
                input_rows=input_rows,
                output_rows=len(out_df),
                warnings=warnings,
                error_count=ai_error_count,
                metrics=metrics,
            )
    except Exception as exc:
        if context is not None:
            context.fail_stage("stage2", exc, metrics=getattr(exc, "metrics", metrics))
        raise


if __name__ == "__main__":
    main()
