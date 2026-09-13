# Getting to Western product contract

## Product boundary

Getting to Western is a dedicated transportation feature. It consumes persisted accessibility profiles directly and does not derive trip facts from Ranking v1 signals. Ranked and unranked listings can both show transportation. Ranking's Value, Campus access, and Transit scores remain separate explanations; the amenity ranking component remains unimplemented and null.

The collection endpoint performs one batched profile read and adds a compact `transportation` object to each listing. It contains destination identity, walking/cycling duration and distance, the primary weekday-morning transit duration/walking/transfers/warnings, accessibility freshness, and static-schedule freshness. It contains no itinerary legs, polylines, stop objects, or raw OTP payload.

`GET /api/listings/{listing_id}/accessibility` returns the complete overview with walking, cycling, all six transit periods, quality/reason codes, and schedule metadata. Geometry is requested only after selection:

```text
GET /api/listings/{id}/accessibility?mode=walking
GET /api/listings/{id}/accessibility?mode=cycling
GET /api/listings/{id}/accessibility?mode=transit&time_period=weekday_morning_commute
```

`include_geometry=false` retains itinerary details while omitting encoded polylines. Exact-route responses include the routing fingerprint. No matching property profile produces an explicit unavailable route rather than a zero or estimated trip.

## User experience

Listing cards show a restrained Walk/Bike/Transit summary. Selecting a listing opens a separate Getting to Western section with:

- walking and cycling time/distance;
- six student-readable transit periods;
- total and walking time, transfers, and review warnings;
- public bus route number/name;
- boarding and exit stops;
- scheduled departure time where present; and
- an accessible text itinerary equivalent to the map.

Walking is the initial map mode because it is universally understandable and does not imply that the primary commute should be transit. The mode and period controls are semantic buttons with pressed state and keyboard support. Mode, period, or listing changes clear the old path before the new request. Closing details or losing the selected listing to filtering/pagination also clears it.

Identical route requests are cached only in memory for the browser session, keyed by listing, destination, mode, period, and returned route identity. No route is placed in local storage or a service worker.

## Map behavior

Leaflet decodes only persisted polyline5 legs. There is no frontend route calculation, coordinate interpolation, or straight-line fallback. Walking legs use a short-dash style, cycling uses a long-dash style, and bus legs are solid, so mode is not communicated by colour alone. Bounds fit the actual decoded path once per selected result with restrained padding.

The selected listing is excluded from the normal marker cluster and rendered in a dedicated overlay, ensuring it remains visible even when its ordinary marker was clustered. A selected destination marker is rendered separately. This hotspot-based contract supports more destinations without coupling route code to the Western marker.

## Special and freshness states

- `walking_better_than_transit`: explain that walking is faster/more practical; draw no fake transit path.
- `no_route`: explain that no useful scheduled transit route was found; do not show zero minutes.
- `partial`: show the real available representative itinerary and its review warning.
- `technical_failure`: describe temporary routing unavailability, not a transit-service finding.
- missing/rejected property: show “Transportation estimate unavailable.”
- `high_walking_share`: retain the trip but warn that it requires substantial walking.

Accessibility expiry and transit-schedule freshness are separate. A current route computed from the active bundle can still carry `stale_schedule` when the underlying static GTFS service range has ended. The detail view calls this historical service guidance and always says transit times are scheduled estimates from static London Transit data, not live departures. The product does not claim bus GPS, delays, cancellations, detours, or GTFS-Realtime.
