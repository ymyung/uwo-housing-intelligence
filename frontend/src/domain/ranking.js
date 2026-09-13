import { numberOrNull } from "./listings.js";

const COMPONENTS = Object.freeze([
  ["value", "Value"],
  ["campus_access", "Campus access"],
  ["transit", "Transit"],
  ["amenities", "Amenities"],
]);

const PERIOD_LABELS = Object.freeze({
  weekday_morning_commute: "Weekday morning",
  weekday_midday: "Weekday midday",
  weekday_evening_commute: "Weekday evening",
  weekday_late_evening: "Weekday late evening",
  saturday_daytime: "Saturday daytime",
  sunday_daytime: "Sunday daytime",
});

const WARNING_LABELS = Object.freeze({
  high_walking_share: "Some transit trips require substantial walking.",
  insufficient_samples: "Some transit periods have limited route evidence.",
  no_route: "No useful transit route was found for some sampled departures.",
  deterministic_no_route: "No useful transit route was found for some sampled departures.",
  walking_better_than_transit: "Walking is faster or more practical for some sampled trips.",
  manual_review: "This listing has a manual review flag.",
  "manual-review": "This listing has a manual review flag.",
});

const ELIGIBILITY_LABELS = Object.freeze({
  listing_not_active: "The listing is not currently active.",
  missing_property: "A verified property could not be linked to this listing.",
  incomplete_trusted_accessibility: "Verified campus and transit data is incomplete.",
  missing_monthly_price: "A reliable monthly price is unavailable.",
  implausible_monthly_price: "The monthly price needs review before ranking.",
  missing_bedrooms: "The bedroom count is unavailable.",
  missing_housing_type: "The housing type is unavailable.",
  missing_rental_structure: "The rental structure is unavailable.",
});

function sentenceFromCode(code, fallback) {
  const key = String(code || "").trim();
  if (!key) return null;
  if (fallback[key]) return fallback[key];
  const text = key.replaceAll("_", " ").replaceAll("-", " ");
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}.`;
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

export function displayScore(value) {
  const score = numberOrNull(value);
  return score === null ? null : `${Math.round(score)}/100`;
}

export function formatCurrency(value) {
  const amount = numberOrNull(value);
  return amount === null
    ? null
    : new Intl.NumberFormat("en-CA", {
        style: "currency",
        currency: "CAD",
        maximumFractionDigits: 0,
      }).format(amount);
}

function componentModels(ranking) {
  const scores = ranking?.components || {};
  return COMPONENTS.map(([key, label]) => ({
    key,
    label,
    score: numberOrNull(scores[key]),
    scoreText: displayScore(scores[key]),
    unavailableText:
      key === "amenities" && ranking?.amenity_status === "not_implemented"
        ? "Not available yet"
        : "Unavailable",
  }));
}

function valueExplanation(ranking) {
  const signal = ranking?.signals?.value;
  if (!signal) return null;
  const price = formatCurrency(signal.monthly_price);
  const median = formatCurrency(signal.market_median);
  const delta = numberOrNull(signal.price_delta_percent);
  const parts = [];
  if (price && median) parts.push(`${price}/month compared with a ${median} market median`);
  else if (price) parts.push(`${price}/month`);
  if (delta !== null) {
    const direction = delta < 0 ? "below" : delta > 0 ? "above" : "at";
    parts.push(
      direction === "at"
        ? "at the comparison median"
        : `${Math.abs(delta).toFixed(1)}% ${direction} the comparison median`,
    );
  }
  return parts.length ? `Value evidence: ${parts.join("; ")}.` : null;
}

function priceComparison(ranking) {
  const signal = ranking?.signals?.value;
  if (!signal) return null;
  const price = numberOrNull(signal.monthly_price);
  const median = numberOrNull(signal.market_median);
  const delta = numberOrNull(signal.price_delta_percent);
  if (price === null || price < 0 || median === null || median <= 0 || delta === null) {
    return null;
  }
  if (Math.abs(delta) < 0.5) {
    return {
      label: "Price comparison",
      detail: "Rent is about the median for listings in this comparison group",
    };
  }
  const direction = delta < 0 ? "below" : "above";
  return {
    label: delta < 0 ? "Price advantage" : "Price comparison",
    detail: `Rent is ${Math.round(Math.abs(delta))}% ${direction} the median for listings in this comparison group`,
  };
}

function campusExplanation(ranking) {
  const signal = ranking?.signals?.campus_access;
  if (!signal) return null;
  const walking = numberOrNull(signal.walking_minutes);
  const cycling = numberOrNull(signal.cycling_minutes);
  const parts = [];
  if (walking !== null) parts.push(`${Math.round(walking)} min walking`);
  if (cycling !== null) parts.push(`${Math.round(cycling)} min cycling`);
  return parts.length ? `Campus access evidence: ${parts.join("; ")}.` : null;
}

function transitExplanations(ranking) {
  const periods = Array.isArray(ranking?.signals?.transit_periods)
    ? ranking.signals.transit_periods
    : [];
  return periods.map((period, index) => {
    const label = PERIOD_LABELS[period.time_period] || `Transit period ${index + 1}`;
    const minutes = numberOrNull(period.representative_minutes);
    const scoreText = displayScore(period.score);
    const facts = [];
    if (minutes !== null) facts.push(`${Math.round(minutes)} representative minutes`);
    if (scoreText) facts.push(`${scoreText} period score`);
    if (numberOrNull(period.transfers) !== null) {
      const transfers = Number(period.transfers);
      facts.push(`${transfers} transfer${transfers === 1 ? "" : "s"}`);
    }
    return {
      key: `${period.time_period || "period"}-${index}`,
      label,
      detail: facts.length ? facts.join(" · ") : "Route evidence unavailable",
      warnings: unique(
        (Array.isArray(period.reason_codes) ? period.reason_codes : [])
          .map((code) => sentenceFromCode(code, WARNING_LABELS)),
      ),
    };
  });
}

function warningMessages(ranking) {
  const warnings = ranking?.warnings || {};
  const reasonCodes = Array.isArray(warnings.accessibility_reason_codes)
    ? warnings.accessibility_reason_codes
    : [];
  const reviewFlags = Array.isArray(warnings.listing_review_flags)
    ? warnings.listing_review_flags
    : [];
  const messages = reasonCodes.map(
    (code) => WARNING_LABELS[code] || "Some accessibility evidence has an additional review note.",
  );
  if (warnings.accessibility_review_required) {
    messages.push("Transit evidence has a review flag.");
  }
  if (reviewFlags.length > 0) {
    messages.push("Some listing details need manual review.");
  }
  return unique(messages);
}

function eligibilityMessages(ranking) {
  const reasons = Array.isArray(ranking?.eligibility_reasons)
    ? ranking.eligibility_reasons
    : [];
  return unique(reasons.map((code) => sentenceFromCode(code, ELIGIBILITY_LABELS)));
}

function studentHighlights(ranking) {
  const allowedKinds = new Set(["campus_access", "transit"]);
  if (!Array.isArray(ranking?.reasons)) return [];
  return ranking.reasons
    .filter(
      (reason) =>
        reason &&
        allowedKinds.has(reason.kind) &&
        typeof reason.title === "string" &&
        typeof reason.detail === "string",
    )
    .map(({ kind, title, detail }) => ({ kind, title, detail }));
}

export function rankingViewModel(ranking) {
  if (!ranking) {
    return {
      status: "unavailable",
      statusLabel: "Ranking unavailable",
      statusDetail: "No current Ranking v1 result is available for this listing.",
      overall: null,
      overallText: null,
      priceComparison: null,
      components: componentModels(null),
      highlights: [],
      explanations: [],
      transitPeriods: [],
      warnings: [],
      eligibility: [],
    };
  }

  const status = ["ranked", "partial", "excluded"].includes(ranking.status)
    ? ranking.status
    : "unavailable";
  const overall = status === "ranked" ? numberOrNull(ranking.overall_score) : null;
  const labels = {
    ranked: ["Overall match", "Based on available price and accessibility information."],
    partial: ["Limited ranking data", "Useful evidence exists, but no overall score was assigned."],
    excluded: ["Not ranked", "Verified information is incomplete, so no overall score was assigned."],
    unavailable: ["Ranking unavailable", "No usable current Ranking v1 result is available for this listing."],
  };
  const [statusLabel, statusDetail] = labels[status];
  return {
    status,
    statusLabel,
    statusDetail,
    overall,
    overallText: displayScore(overall),
    priceComparison: priceComparison(ranking),
    components: componentModels(ranking),
    highlights: studentHighlights(ranking),
    explanations: unique([valueExplanation(ranking), campusExplanation(ranking)]),
    transitPeriods: transitExplanations(ranking),
    warnings: warningMessages(ranking),
    eligibility: eligibilityMessages(ranking),
  };
}
