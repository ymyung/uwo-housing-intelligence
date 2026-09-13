import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  DEFAULT_FILTERS,
  SORT_OPTIONS,
  apiParameters,
  filtersFromSearch,
  filtersToSearch,
} from "../src/domain/listings.js";
import { rankingViewModel } from "../src/domain/ranking.js";

function rankingFixture(overrides = {}) {
  return {
    version: "ranking-v1",
    status: "ranked",
    overall_score: 84.44,
    components: {
      value: 91.2,
      campus_access: 82.1,
      transit: 74.4,
      amenities: null,
      data_quality: null,
    },
    weights: { value: 0.45, campus_access: 0.35, transit: 0.2, amenities: 0 },
    signals: {
      value: {
        monthly_price: 850,
        market_median: 975,
        price_delta_percent: -12.82,
      },
      campus_access: { walking_minutes: 18, cycling_minutes: 7 },
      transit_periods: [
        {
          time_period: "weekday_morning_commute",
          score: 74.4,
          representative_minutes: 31,
          transfers: 1,
          reason_codes: [],
        },
      ],
    },
    warnings: {
      accessibility_reason_codes: [],
      accessibility_review_required: false,
      listing_review_flags: [],
    },
    reasons: [
      { kind: "value", title: "Good value", detail: "13% below the comparison median" },
      { kind: "campus_access", title: "Campus commute", detail: "18 min walk to Western" },
      { kind: "transit", title: "Morning transit", detail: "31 min typical weekday morning trip" },
    ],
    eligibility_reasons: [],
    unavailable_components: ["amenities"],
    amenity_status: "not_implemented",
    data_quality_status: "not_scored",
    ...overrides,
  };
}

test("ranked model presents authoritative overall and component scores", () => {
  const model = rankingViewModel(rankingFixture());
  assert.equal(model.statusLabel, "Overall match");
  assert.equal(model.overall, 84.44);
  assert.equal(model.overallText, "84/100");
  assert.deepEqual(
    model.components.slice(0, 3).map(({ label, scoreText }) => [label, scoreText]),
    [["Value", "91/100"], ["Campus access", "82/100"], ["Transit", "74/100"]],
  );
  assert.equal(model.components[3].scoreText, null);
  assert.equal(model.components[3].unavailableText, "Not available yet");
});

test("price comparison is distinct from the independent overall match score", () => {
  const model = rankingViewModel(rankingFixture({
    overall_score: 79,
    signals: {
      value: {
        monthly_price: 600,
        market_median: 810,
        price_delta_percent: -25.93,
      },
    },
  }));

  assert.equal(model.overallText, "79/100");
  assert.deepEqual(model.priceComparison, {
    label: "Price advantage",
    detail: "Rent is 26% below the median for listings in this comparison group",
  });
  assert.doesNotMatch(model.priceComparison.detail, /score|79/);
});

test("above-median rent uses neutral price-comparison wording", () => {
  const model = rankingViewModel(rankingFixture({
    signals: {
      value: {
        monthly_price: 1120,
        market_median: 1000,
        price_delta_percent: 12,
      },
    },
  }));

  assert.deepEqual(model.priceComparison, {
    label: "Price comparison",
    detail: "Rent is 12% above the median for listings in this comparison group",
  });
});

test("rent near the comparison median uses neutral approximate wording", () => {
  const model = rankingViewModel(rankingFixture({
    signals: {
      value: {
        monthly_price: 1002,
        market_median: 1000,
        price_delta_percent: 0.2,
      },
    },
  }));

  assert.deepEqual(model.priceComparison, {
    label: "Price comparison",
    detail: "Rent is about the median for listings in this comparison group",
  });
});

test("missing rent or comparison data never creates an unsupported price claim", () => {
  for (const value of [
    { market_median: 1000, price_delta_percent: -20 },
    { monthly_price: 800, price_delta_percent: -20 },
    { monthly_price: 800, market_median: 1000 },
  ]) {
    const model = rankingViewModel(rankingFixture({ signals: { value } }));
    assert.equal(model.priceComparison, null);
  }
});

test("partial, excluded, and absent rankings never create a fake zero score", () => {
  const partial = rankingViewModel(rankingFixture({ status: "partial", overall_score: null }));
  const excluded = rankingViewModel(rankingFixture({ status: "excluded", overall_score: null }));
  const unavailable = rankingViewModel(null);
  assert.equal(partial.statusLabel, "Limited ranking data");
  assert.equal(excluded.statusLabel, "Not ranked");
  for (const model of [partial, excluded, unavailable]) {
    assert.equal(model.overall, null);
    assert.equal(model.overallText, null);
    assert.doesNotMatch(JSON.stringify(model), /NaN|0\/100/);
  }
});

test("explanations format API signals without deriving replacement scores", () => {
  const model = rankingViewModel(rankingFixture());
  assert.deepEqual(model.highlights, [
    { kind: "campus_access", title: "Campus commute", detail: "18 min walk to Western" },
    { kind: "transit", title: "Morning transit", detail: "31 min typical weekday morning trip" },
  ]);
  assert.match(model.explanations[0], /\$850\/month/);
  assert.match(model.explanations[0], /\$975 market median/);
  assert.match(model.explanations[0], /12\.8% below/);
  assert.equal(model.explanations[1], "Campus access evidence: 18 min walking; 7 min cycling.");
  assert.equal(model.transitPeriods[0].detail, "31 representative minutes · 74/100 period score · 1 transfer");
});

test("missing reasons and partial ranking remain truthful", () => {
  const partial = rankingViewModel(rankingFixture({
    status: "partial",
    overall_score: null,
    reasons: [{ kind: "campus_access", title: "Campus commute", detail: "18 min walk to Western" }],
  }));
  const excluded = rankingViewModel(rankingFixture({ status: "excluded", overall_score: null, reasons: [] }));
  assert.equal(partial.statusLabel, "Limited ranking data");
  assert.equal(partial.highlights[0].detail, "18 min walk to Western");
  assert.equal(excluded.statusLabel, "Not ranked");
  assert.deepEqual(excluded.highlights, []);
});

test("accessibility findings and review flags become calm student-facing notes", () => {
  const model = rankingViewModel(rankingFixture({
    warnings: {
      accessibility_reason_codes: ["high_walking_share", "walking_better_than_transit", "no_route"],
      accessibility_review_required: true,
      listing_review_flags: ["manual-review"],
    },
  }));
  assert.ok(model.warnings.includes("Some transit trips require substantial walking."));
  assert.ok(model.warnings.includes("Walking is faster or more practical for some sampled trips."));
  assert.ok(model.warnings.includes("No useful transit route was found for some sampled departures."));
  assert.ok(model.warnings.includes("Some listing details need manual review."));
  assert.ok(model.warnings.includes("Transit evidence has a review flag."));
  assert.doesNotMatch(model.warnings.join(" "), /provider error/i);
});

test("ranking controls round-trip through the centralized server query model", () => {
  const filters = {
    ...DEFAULT_FILTERS,
    bedrooms: "4",
    max_price: "900",
    ranking_status: "ranked",
    min_score: "60",
  };
  const query = filtersToSearch(filters, {
    sort: "transit_score",
    hotspotId: "western-main-campus",
    page: 3,
  });
  const restored = filtersFromSearch(query);
  const api = apiParameters(restored, {
    sort: "transit_score",
    hotspotId: "western-main-campus",
    page: 3,
  });
  assert.equal(api.bedrooms, "4");
  assert.equal(api.max_price, "900");
  assert.equal(api.ranking_status, "ranked");
  assert.equal(api.min_score, "60");
  assert.equal(api.sort, "transit_score");
  assert.equal(api.page, 3);
  assert.doesNotMatch(query, /min_value_score/);
});

test("all user-facing ranking sorts map to validated backend keys", () => {
  assert.deepEqual(
    SORT_OPTIONS.slice(0, 5),
    [
      ["recommended", "Recommended"],
      ["overall_score", "Best overall match"],
      ["value_score", "Best value"],
      ["campus_access_score", "Best campus access"],
      ["transit_score", "Best transit"],
    ],
  );
});

test("card and detail use one model, native disclosure, and server-backed controls", async () => {
  const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
  const display = await readFile(new URL("../src/components/RankingDisplay.jsx", import.meta.url), "utf8");
  const model = await readFile(new URL("../src/domain/ranking.js", import.meta.url), "utf8");
  assert.match(app, /<RankingCardSummary ranking=\{listing\.ranking\}/);
  assert.match(app, /<RankingDetails ranking=\{listing\.ranking\}/);
  assert.match(display, /rankingViewModel\(ranking\)/);
  assert.match(display, /Why this listing\?/);
  assert.match(display, /<details className="ranking-explanation">/);
  assert.match(display, /<summary>More ranking evidence<\/summary>/);
  assert.match(app, /function changeSort\(value\)/);
  assert.match(app, /setSort\(value\);[\s\S]*setPage\(1\);/);
  assert.match(app, /updateFilter\("min_score"/);
  assert.doesNotMatch(model, /component_weights|ranking\.weights|0\.45|0\.35|0\.2/);
  assert.doesNotMatch(app, /\.sort\([^)]*ranking|score_listings|DEFAULT_RANKING_WEIGHTS/);
});
