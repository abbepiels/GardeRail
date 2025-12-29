# GardeRail Allergen Verification (MVP)

## Investor Overview
- Purpose: Verify food orders against declared allergies before checkout, using upstream supplier truth (GTIN + lot), location inventory scans, and explicit user consent.
- How it works: Cart -> menu item -> recipe base ingredients -> active scanned products (GTIN+lot) -> supplier truth (allergens/risk/recall) -> return SAFE/UNSAFE/UNKNOWN; stale/missing data defaults to UNKNOWN (never over-claim safe).
- Consent gate: Checkout is impossible until the user accepts the risk with a time-limited token tied to the audit.
- Trust & transparency: Every verification produces an audit (base ingredients, active GTIN/lot links, versions, mapping trace, policy settings).
- Demo scope: Single-file FastAPI app, in-memory catalogs/tokens/audits; easily replaceable with real services later.

## Technical Overview
- Stack: Python 3.11+, FastAPI, Pydantic; single file `app.py`.
- Allergen model: Major 9 allergens; states CONTAINS/MAY_CONTAIN/NOT_PRESENT/UNKNOWN; policy flag for MAY_CONTAIN -> unsafe (default false); stale data threshold (30 days).
- Supplier truth: In-memory catalog keyed by `(gtin, lot)` with allergens, risk flags, version, updated_at, recall flag; GET `/v1/supplier/truth`.
- Inventory scans: `POST /v1/inventory/scan` binds a scanned GTIN+lot to a base ingredient at a location; `GET /v1/inventory/active` lists active products per location/base ingredient.
- Menu & recipes: Stored in-memory (MenuItem with modifiers/options, Recipe + RecipeLine, ModifierRule). Seeded with `chicken_burrito` (modifiers: tortilla flour/corn, cheese yes/no, sauce none/chipotle_mayo). `/v1/menu` returns the store-backed menu; `/v1/verify` resolves ingredients via recipe + modifier rules (ADD/REMOVE/REPLACE) before checking inventory.
- Integrations/imports: Simple JSON import endpoints — Toast-style menu `POST /v1/integrations/toast/menu/import` (list of MenuItem), xtraCHEF ingredients `POST /v1/integrations/xtrachef/ingredients/import` (Ingredient + optional VendorProduct), xtraCHEF recipes `POST /v1/integrations/xtrachef/recipes/import` (Recipe with RecipeLine). Status via `GET /v1/integrations/status` (counts + last_import timestamps). All stores remain in-memory.
- Verification: `POST /v1/verify` (requires `location_id`) resolves base ingredients, pulls active GTIN+lot from inventory, checks supplier truth (including recall and stale), applies policy (contains/recall -> UNSAFE; missing/stale/unknown -> UNKNOWN unless already unsafe), can enrich allergen truth via public UPC providers, and returns decision/summary/reasons plus `audit_id` and short-lived `checkout_token`.
- Consent & checkout: POST `/v1/consent` requires matching audit/token, unexpired, accepted=true; stamps consent. POST `/v1/checkout` rejects without consent (403) and returns audit/decision when permitted.
- Audit: `/v1/audit/{audit_id}` returns request payload, location_id, decision, reasons, mapping trace, base ingredients, active product links, supplier versions, cart hash, policy; `/v1/audit` lists the latest audit id.
- Distributor mock (legacy): In-memory SKU catalog with GET `/v1/distributor/products/{sku}` remains for compatibility; verification no longer depends on it.
- Public data providers: In-memory adapter layer for UPC lookups via USDA FoodData Central (API key, DEMO_KEY default), OpenFoodFacts (public, sets User-Agent), SmartLabel stub (requires credentials; not implemented). Provider readiness: `GET /v1/providers`. UPC lookup: `GET /v1/lookup/upc/{upc}`.

## Run & Demo
- Install: `pip install fastapi uvicorn`
- Start: `uvicorn app:app --reload`
- Try in browser: Swagger UI at http://localhost:8000/docs
- Demo flow (also in `app.py` header):
  1) Seed inventory for `store_1` with scans:
     - `POST /v1/inventory/scan` for `TORTILLA_FLOUR` (gtin `000111FLOUR`, lot `L1`), `TORTILLA_CORN` (`000222CORN`/`L2`), `MAYO` (`000333MAYO`/`M1`, recall demo), `CHEESE` (`000444CHEESE`/`C1`), `CHICKEN` (`000555CHICKEN`/`K1`).
  2) Verify unsafe: wheat allergy + flour tortilla => UNSAFE (WHEAT in flour tortilla, recall demo on mayo shows recall reason if sauce chosen).
  3) Verify safe: switch to corn tortilla and drop mayo => SAFE.
  4) Consent + checkout: call `/v1/verify`, then `/v1/consent` with the returned token/audit, then `/v1/checkout`.
  5) Optional imports: replace seeded menu/recipes/ingredients with your own using the integration endpoints (Toast menu import, xtraCHEF ingredients/recipes import), then check `GET /v1/integrations/status`.
  6) UPC lookups: `GET /v1/lookup/upc/048001214101` (Hellmann’s UPC) or list providers via `GET /v1/providers`.
