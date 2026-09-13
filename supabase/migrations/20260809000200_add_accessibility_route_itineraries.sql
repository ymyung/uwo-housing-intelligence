-- Compact normalized route itineraries for the Getting to Western product.
-- Direct routes live on profiles. Transit geometries live only on the actual
-- sampled departure that supplied the representative profile values.

alter table public.housing_accessibility_profiles
    add column representative_sample_departure_at timestamptz,
    add column route_itinerary jsonb;

alter table public.housing_accessibility_samples
    add column route_itinerary jsonb;

alter table public.housing_accessibility_profiles
    add constraint housing_accessibility_profile_route_itinerary_object_check
        check (
            route_itinerary is null
            or jsonb_typeof(route_itinerary) = 'object'
        ),
    add constraint housing_accessibility_representative_sample_mode_check
        check (
            representative_sample_departure_at is null
            or travel_mode = 'transit'
        );

alter table public.housing_accessibility_samples
    add constraint housing_accessibility_sample_route_itinerary_object_check
        check (
            route_itinerary is null
            or jsonb_typeof(route_itinerary) = 'object'
        );

comment on column public.housing_accessibility_profiles.route_itinerary is
    'Normalized direct walk/cycle itinerary with compact encoded leg geometry; never a raw provider payload.';
comment on column public.housing_accessibility_profiles.representative_sample_departure_at is
    'Actual persisted transit sample selected deterministically for representative metrics and map geometry.';
comment on column public.housing_accessibility_samples.route_itinerary is
    'Normalized itinerary for this sampled departure, including compact encoded geometry and student-useful leg details.';
