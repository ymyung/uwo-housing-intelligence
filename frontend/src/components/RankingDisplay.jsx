import { rankingViewModel } from "../domain/ranking.js";

export function RankingCardSummary({ ranking }) {
  const model = rankingViewModel(ranking);
  const highlight = model.highlights[0];
  return (
    <section className={`ranking-summary ranking-${model.status}`} aria-label="Listing ranking">
      <div className="ranking-summary-heading">
        <span>{model.statusLabel}</span>
        {model.overallText && <strong>{model.overallText}</strong>}
      </div>
      {model.priceComparison ? (
        <div className="ranking-price-comparison ranking-price-comparison-compact">
          <strong>{model.priceComparison.label}</strong>
          <span>{model.priceComparison.detail}</span>
        </div>
      ) : highlight && <p className="ranking-card-reason">{highlight.detail}</p>}
    </section>
  );
}

export function RankingDetails({ ranking }) {
  const model = rankingViewModel(ranking);
  return (
    <section className={`ranking-detail ranking-${model.status}`} aria-labelledby="ranking-title">
      <div className="ranking-detail-heading">
        <div>
          <p className="eyebrow">Ranking v1</p>
          <h3 id="ranking-title">{model.statusLabel}</h3>
        </div>
        {model.overallText && <strong className="ranking-overall">{model.overallText}</strong>}
      </div>
      <p className="ranking-status-detail">{model.statusDetail}</p>

      {model.priceComparison && (
        <div className="ranking-price-comparison">
          <strong>{model.priceComparison.label}</strong>
          <span>{model.priceComparison.detail}</span>
        </div>
      )}

      {model.highlights.length > 0 && (
        <div className="ranking-highlights" aria-label="Why this listing">
          <h4>Why this listing?</h4>
          {model.highlights.map((highlight) => (
            <div className="ranking-highlight" key={highlight.kind}>
              <strong>{highlight.title}</strong>
              <span>{highlight.detail}</span>
            </div>
          ))}
        </div>
      )}

      {(model.explanations.length > 0 || model.transitPeriods.length > 0) && (
        <details className="ranking-explanation">
          <summary>More ranking evidence</summary>
          <div className="ranking-explanation-body">
            {model.explanations.map((explanation) => <p key={explanation}>{explanation}</p>)}
            {model.transitPeriods.length > 0 && (
              <div>
                <h4>Transit periods</h4>
                <ul className="ranking-periods">
                  {model.transitPeriods.map((period) => (
                    <li key={period.key}>
                      <strong>{period.label}</strong>
                      <span>{period.detail}</span>
                      {period.warnings.map((warning) => <small key={warning}>{warning}</small>)}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </details>
      )}

      {model.eligibility.length > 0 && (
        <div className="ranking-notice">
          <strong>Why there is no overall score</strong>
          <ul>{model.eligibility.map((message) => <li key={message}>{message}</li>)}</ul>
        </div>
      )}
      {model.warnings.length > 0 && (
        <div className="ranking-notice ranking-warning">
          <strong>Evidence notes</strong>
          <ul>{model.warnings.map((message) => <li key={message}>{message}</li>)}</ul>
        </div>
      )}
    </section>
  );
}
