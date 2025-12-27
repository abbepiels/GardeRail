# GardeRail Allergen Verification (MVP)

## Investor Overview
- Purpose: Verify food orders against declared allergies before checkout, using upstream ingredient truth and explicit user consent.
- How it works: Cart -> resolve to real ingredient SKUs -> check distributor allergen data -> return SAFE/UNSAFE/UNKNOWN; stale/missing data defaults to UNKNOWN (never over-claim safe).
- Consent gate: Checkout is impossible until the user accepts the risk with a time-limited token tied to the audit.
- Trust & transparency: Every verification produces an audit (ingredients used, versions, mapping trace, policy settings).
- Demo scope: Single-file FastAPI app, in-memory catalogs/tokens/audits; easily replaceable with real services later.

## Technical Overview
- Stack: Python 3.11+, FastAPI, Pydantic; single file `app.py`.
- Allergen model: Major 9 allergens; states CONTAINS/MAY_CONTAIN/NOT_PRESENT/UNKNOWN; policy flag for MAY_CONTAIN -> unsafe (default false); stale data threshold (30 days).
- Distributor mock: In-memory catalog with SKUs (e.g., `MISSION_FLOUR_TORTILLA_12IN`, `UNILEVER_HELLMANNS_MAYO_30OZ`), version, updated_at, allergens; GET `/v1/distributor/products/{sku}`.
- Menu & recipes: `/v1/menu` exposes a Chipotle-like item (chicken_burrito) with modifiers; recipe rules map modifiers to ingredient SKUs with trace for auditing.
- Verification: POST `/v1/verify` resolves SKUs, fetches distributor data, applies policy (contains -> UNSAFE; stale/missing/unknown -> UNKNOWN unless already unsafe), returns decision/summary/reasons plus `audit_id` and short-lived `checkout_token`.
- Consent & checkout: POST `/v1/consent` requires matching audit/token, unexpired, accepted=true; stamps consent. POST `/v1/checkout` rejects without consent (403) and returns audit/decision when permitted.
- Audit: `/v1/audit/{audit_id}` returns request payload, decision, reasons, mapping trace, ingredient versions, cart hash, policy; `/v1/audit` lists the latest audit id.

## Run & Demo
- Install: `pip install fastapi uvicorn`
- Start: `uvicorn app:app --reload`
- Try in browser: Swagger UI at http://localhost:8000/docs
- Quick curls (also in `app.py` header):
  - Unsafe (wheat + flour tortilla) -> UNSAFE
  - Safe (wheat + corn tortilla) -> SAFE
  - Consent + checkout: call `/v1/verify`, then `/v1/consent` with the returned token/audit, then `/v1/checkout`.
