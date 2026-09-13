# Technical Overview

## Project Scope

The Western Housing Intelligence Platform began as a housing-data pipeline and evolved into a full-stack housing discovery and decision-support system.

The primary technical challenge is not simply displaying rental listings.

The project focuses on maintaining reliable structured housing data while combining it with:

- transportation
- geospatial analysis
- historical observations
- ranking
- comparison
- location-quality controls
- municipal reference data

---

# Major Engineering Problems Solved

## 1. Converting Semi-Structured Listings into Reliable Data

Housing advertisements contain a mixture of:

- structured fields
- free-form descriptions
- inconsistent price formats
- incomplete lease information
- ambiguous furnishing information
- roommate preferences
- utilities information
- inconsistent availability language

The pipeline converts these sources into explicit structured fields.

A data-evidence hierarchy prevents weaker inferred information from overriding stronger source evidence.

---

## 2. Preserving Unknown Values Correctly

A recurring rule throughout the project is:

```text
unknown != false