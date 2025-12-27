# GardeRail FastAPI MVP
# Install/run: pip install fastapi uvicorn; uvicorn app:app --reload
# Example demo flow:
# 1) Seed inventory by scanning GTIN/lots to base ingredients for location store_1:
# curl -X POST http://localhost:8000/v1/inventory/scan -H "Content-Type: application/json" \
#  -d '{"location_id":"store_1","gtin":"000111FLOUR","lot":"L1","base_ingredient_id":"TORTILLA_FLOUR"}'
# curl -X POST http://localhost:8000/v1/inventory/scan -H "Content-Type: application/json" \
#  -d '{"location_id":"store_1","gtin":"000222CORN","lot":"L2","base_ingredient_id":"TORTILLA_CORN"}'
# curl -X POST http://localhost:8000/v1/inventory/scan -H "Content-Type: application/json" \
#  -d '{"location_id":"store_1","gtin":"000333MAYO","lot":"M1","base_ingredient_id":"MAYO"}'
# curl -X POST http://localhost:8000/v1/inventory/scan -H "Content-Type: application/json" \
#  -d '{"location_id":"store_1","gtin":"000444CHEESE","lot":"C1","base_ingredient_id":"CHEESE"}'
# curl -X POST http://localhost:8000/v1/inventory/scan -H "Content-Type: application/json" \
#  -d '{"location_id":"store_1","gtin":"000555CHICKEN","lot":"K1","base_ingredient_id":"CHICKEN"}'
# 2) Verify unsafe with wheat allergy and flour tortilla:
# curl -X POST http://localhost:8000/v1/verify -H "Content-Type: application/json" \
#  -d '{"user_id":"u1","location_id":"store_1","allergies":["WHEAT"],"cart":[{"item_id":"chicken_burrito","qty":1,"modifiers":{"tortilla":"flour","cheese":"yes","sauce":"chipotle_mayo"}}]}'
# 3) Switch tortilla to corn (scan above already set) and verify safe:
# curl -X POST http://localhost:8000/v1/verify -H "Content-Type: application/json" \
#  -d '{"user_id":"u1","location_id":"store_1","allergies":["WHEAT"],"cart":[{"item_id":"chicken_burrito","qty":1,"modifiers":{"tortilla":"corn","cheese":"no","sauce":"none"}}]}'
# Lookup a UPC via public providers:
# curl http://localhost:8000/v1/lookup/upc/048001214101
# List provider readiness:
# curl http://localhost:8000/v1/providers
# Consent then checkout remains unchanged:
# TOKEN=$(curl -s -X POST http://localhost:8000/v1/verify -H "Content-Type: application/json" \
#  -d '{"user_id":"u1","location_id":"store_1","allergies":["WHEAT"],"cart":[{"item_id":"chicken_burrito","qty":1,"modifiers":{"tortilla":"corn","cheese":"no","sauce":"none"}}]}' | python -c "import sys,json;print(json.load(sys.stdin)['checkout_token'])") && \
# AUDIT=$(curl -s http://localhost:8000/v1/audit | python - <<'PY'\nimport sys,json\nprint(json.load(sys.stdin)['latest_audit_id'])\nPY)
# curl -X POST http://localhost:8000/v1/consent -H "Content-Type: application/json" -d "{\"checkout_token\":\"$TOKEN\",\"audit_id\":\"$AUDIT\",\"accepted\":true}"
# curl -X POST http://localhost:8000/v1/checkout -H "Content-Type: application/json" -d "{\"checkout_token\":\"$TOKEN\"}"

import os
from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

import urllib.error
import urllib.request

from fastapi import FastAPI, HTTPException, Path, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator


class Allergen(str, Enum):
    MILK = "MILK"
    EGG = "EGG"
    FISH = "FISH"
    SHELLFISH = "SHELLFISH"
    TREE_NUT = "TREE_NUT"
    PEANUT = "PEANUT"
    WHEAT = "WHEAT"
    SOY = "SOY"
    SESAME = "SESAME"


class AllergenState(str, Enum):
    CONTAINS = "CONTAINS"
    MAY_CONTAIN = "MAY_CONTAIN"
    NOT_PRESENT = "NOT_PRESENT"
    UNKNOWN = "UNKNOWN"


class VerificationDecision(str, Enum):
    SAFE = "SAFE"
    UNSAFE = "UNSAFE"
    UNKNOWN = "UNKNOWN"


class ProductSource(str, Enum):
    SEEDED = "SEEDED"
    USDA_FDC = "USDA_FDC"
    OPENFOODFACTS = "OPENFOODFACTS"
    SMARTLABEL = "SMARTLABEL"


class DistributorProduct(BaseModel):
    sku: str
    brand: str
    name: str
    upc: Optional[str] = None
    allergens: Dict[Allergen, AllergenState]
    version: str
    updated_at: datetime
    fetched_at: Optional[datetime] = None
    upstream_updated_at: Optional[datetime] = None
    source: ProductSource = ProductSource.SEEDED


class SupplierProductTruth(BaseModel):
    gtin: str
    lot: str
    brand: str
    name: str
    upc: Optional[str] = None
    allergens: Dict[Allergen, AllergenState]
    risk_flags: List[str] = Field(default_factory=list)
    version: str
    updated_at: datetime
    recall: Optional[bool] = False


class MenuModifierOption(BaseModel):
    value: str
    label: str


class MenuModifier(BaseModel):
    name: str
    options: List[MenuModifierOption]
    default: Optional[str] = None


class MenuItem(BaseModel):
    item_id: str
    name: str
    modifiers: List[MenuModifier]


class VerifyCartItem(BaseModel):
    item_id: str
    qty: int = Field(ge=1, default=1)
    modifiers: Dict[str, str] = Field(default_factory=dict)


class VerifyRequest(BaseModel):
    user_id: str
    location_id: str
    allergies: List[Allergen]
    cart: List[VerifyCartItem]
    channel: str = "ubereats"

    @validator("allergies", each_item=True)
    def ensure_canonical_allergen(cls, v):
        if isinstance(v, Allergen):
            return v
        raise ValueError("Invalid allergen")


class Reason(BaseModel):
    allergen: Allergen
    ingredient_sku: str
    ingredient_name: str
    state: AllergenState
    explanation: str
    provider: Optional[str] = None
    provider_fetched_at: Optional[datetime] = None


class MappingRuleTrace(BaseModel):
    rule: str
    matched: bool
    added_skus: List[str]
    details: Optional[Dict[str, str]] = None


class ItemMappingTrace(BaseModel):
    item_id: str
    base_ingredients: List[str]
    matched_rules: List[MappingRuleTrace]
    final_base_ingredients: List[str]


class VerifyResponse(BaseModel):
    decision: VerificationDecision
    summary: str
    reasons: List[Reason]
    audit_id: str
    checkout_token: str
    token_expires_at: datetime


class ConsentRequest(BaseModel):
    checkout_token: str
    audit_id: str
    accepted: bool


class ConsentResponse(BaseModel):
    ok: bool
    consented_at: datetime
    audit_id: str


class CheckoutRequest(BaseModel):
    checkout_token: str


class CheckoutResponse(BaseModel):
    ok: bool
    audit_id: str
    decision: VerificationDecision


class InventoryScanRequest(BaseModel):
    location_id: str
    gtin: str
    lot: str
    base_ingredient_id: str


class InventoryScanResponse(BaseModel):
    ok: bool
    location_id: str
    base_ingredient_id: str
    linked_at: datetime
    supplier_truth: SupplierProductTruth


class ActiveProduct(BaseModel):
    base_ingredient_id: str
    gtin: str
    lot: str
    brand: str
    name: str
    upc: Optional[str] = None
    version: str
    updated_at: datetime
    recall: Optional[bool] = False
    risk_flags: List[str] = Field(default_factory=list)
    linked_at: datetime


class InventoryActiveResponse(BaseModel):
    location_id: str
    active: List[ActiveProduct]


class ExternalProductRecord(BaseModel):
    upc: str
    name: Optional[str] = None
    brand_owner: Optional[str] = None
    brand: Optional[str] = None
    ingredients_text: Optional[str] = None
    allergens_detected: List[Allergen] = Field(default_factory=list)
    raw_allergens_text: Optional[str] = None
    raw_allergens_tags: Optional[List[str]] = None
    source: ProductSource
    fetched_at: datetime
    upstream_updated_at: Optional[datetime] = None
    heuristics_used: bool = False
    heuristics_notes: Optional[str] = None
    disclaimer: Optional[str] = None


class AuditRecord(BaseModel):
    audit_id: str
    timestamp: datetime
    consent_timestamp: Optional[datetime] = None
    location_id: str
    request_payload: dict
    decision: VerificationDecision
    reasons: List[Reason]
    mapping_trace: List[ItemMappingTrace]
    ingredient_versions_used: Dict[str, str]
    base_ingredients_used: Dict[str, List[str]]
    active_product_links: Dict[str, Dict[str, str]]
    cart_hash: str
    policy: Dict[str, str]


class AuditResponse(BaseModel):
    audit: AuditRecord


class Config:
    stale_days: int = 30
    may_contain_unsafe: bool = False
    checkout_token_ttl_minutes: int = 10
    provider_cache_ttl_hours: int = 24
    request_timeout_seconds: int = 5


app = FastAPI(title="GardeRail Allergen Verification")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def seeded_products() -> Dict[str, DistributorProduct]:
    now = utcnow()
    return {
        "UNILEVER_HELLMANNS_MAYO_30OZ": DistributorProduct(
            sku="UNILEVER_HELLMANNS_MAYO_30OZ",
            brand="Unilever",
            name="Hellmann's Mayo 30oz",
            upc="048001214101",
            allergens={
                Allergen.EGG: AllergenState.CONTAINS,
                Allergen.SOY: AllergenState.MAY_CONTAIN,
                Allergen.WHEAT: AllergenState.NOT_PRESENT,
            },
            version="1.0.0",
            updated_at=now,
        ),
        "MISSION_FLOUR_TORTILLA_12IN": DistributorProduct(
            sku="MISSION_FLOUR_TORTILLA_12IN",
            brand="Mission",
            name="Flour Tortilla 12in",
            upc="073731009670",
            allergens={Allergen.WHEAT: AllergenState.CONTAINS},
            version="1.0.0",
            updated_at=now,
        ),
        "SUPPLIER_X_CORN_TORTILLA_6IN": DistributorProduct(
            sku="SUPPLIER_X_CORN_TORTILLA_6IN",
            brand="Supplier X",
            name="Corn Tortilla 6in",
            allergens={Allergen.WHEAT: AllergenState.NOT_PRESENT},
            version="1.0.0",
            updated_at=now,
        ),
        "DAIRY_CHEESE_SHRED": DistributorProduct(
            sku="DAIRY_CHEESE_SHRED",
            brand="DairyCo",
            name="Shredded Cheese",
            allergens={
                Allergen.MILK: AllergenState.CONTAINS,
                Allergen.WHEAT: AllergenState.NOT_PRESENT,
            },
            version="1.0.0",
            updated_at=now,
        ),
        "CHIPOTLE_CHICKEN_PREP": DistributorProduct(
            sku="CHIPOTLE_CHICKEN_PREP",
            brand="Chipotle",
            name="Chipotle Chicken Prep",
            allergens={
                Allergen.SOY: AllergenState.MAY_CONTAIN,
                Allergen.WHEAT: AllergenState.NOT_PRESENT,
            },
            version="1.0.0",
            updated_at=now,
        ),
    }


def load_distributor_catalog() -> Dict[str, DistributorProduct]:
    """Fetch distributor catalog from a URL, fallback to seeded data."""
    catalog_url = os.getenv("CATALOG_URL")
    if not catalog_url:
        return seeded_products()

    try:
        with urllib.request.urlopen(catalog_url, timeout=5) as response:
            if response.status != 200:
                raise ValueError(f"unexpected status {response.status}")
            raw_catalog = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to fetch remote catalog, using seeded data: {exc}")
        return seeded_products()

    catalog: Dict[str, DistributorProduct] = {}
    for entry in raw_catalog:
        try:
            allergens = {
                Allergen(key): AllergenState(value)
                for key, value in entry.get("allergens", {}).items()
                if key in Allergen.__members__
            }
            updated_raw = entry.get("updated_at")
            updated_at = datetime.fromisoformat(updated_raw) if updated_raw else utcnow()
            product = DistributorProduct(
                sku=entry["sku"],
                brand=entry.get("brand", ""),
                name=entry.get("name", ""),
                allergens=allergens,
                version=str(entry.get("version", "unknown")),
                updated_at=updated_at,
            )
            catalog[product.sku] = product
        except Exception as exc:  # Keep the server running even with bad rows
            print(f"Skipping invalid catalog entry {entry!r}: {exc}")

    if not catalog:
        print("Remote catalog empty after parsing, falling back to seeded data")
        return seeded_products()

    return catalog


distributor_catalog: Dict[str, DistributorProduct] = load_distributor_catalog()
supplier_truth_catalog: Dict[Tuple[str, str], SupplierProductTruth] = {}
active_products: Dict[Tuple[str, str], Dict[str, object]] = {}
provider_cache_by_upc: Dict[str, Dict[str, object]] = {}
provider_cache_by_sku: Dict[str, Dict[str, object]] = {}
config = Config()


def seeded_supplier_truth() -> Dict[Tuple[str, str], SupplierProductTruth]:
    now = utcnow()
    seeds = [
        SupplierProductTruth(
            gtin="000111FLOUR",
            lot="L1",
            brand="Mission",
            name="Flour Tortilla 12in",
            upc="073731009670",
            allergens={Allergen.WHEAT: AllergenState.CONTAINS},
            risk_flags=["MAY_CONTAIN_SESAME"],
            version="1.0.0",
            updated_at=now,
            recall=False,
        ),
        SupplierProductTruth(
            gtin="000222CORN",
            lot="L2",
            brand="Supplier X",
            name="Corn Tortilla 6in",
            upc="000222000222",
            allergens={Allergen.WHEAT: AllergenState.NOT_PRESENT},
            risk_flags=[],
            version="1.0.0",
            updated_at=now,
            recall=False,
        ),
        SupplierProductTruth(
            gtin="000333MAYO",
            lot="M1",
            brand="Unilever",
            name="Hellmann's Mayo 30oz",
            upc="048001214101",
            allergens={
                Allergen.EGG: AllergenState.CONTAINS,
                Allergen.SOY: AllergenState.MAY_CONTAIN,
            },
            risk_flags=["SHARED_LINE_SOY"],
            version="1.0.0",
            updated_at=now,
            recall=True,
        ),
        SupplierProductTruth(
            gtin="000444CHEESE",
            lot="C1",
            brand="DairyCo",
            name="Shredded Cheese",
            allergens={Allergen.MILK: AllergenState.CONTAINS},
            risk_flags=[],
            version="1.0.0",
            updated_at=now,
            recall=False,
        ),
        SupplierProductTruth(
            gtin="000555CHICKEN",
            lot="K1",
            brand="Chipotle",
            name="Chipotle Chicken Prep",
            allergens={
                Allergen.SOY: AllergenState.MAY_CONTAIN,
                Allergen.WHEAT: AllergenState.NOT_PRESENT,
            },
            risk_flags=["SHARED_LINE_SOY"],
            version="1.0.0",
            updated_at=now,
            recall=False,
        ),
    ]
    return {(prod.gtin, prod.lot): prod for prod in seeds}


# Seed supplier truth on startup for the demo.
supplier_truth_catalog.update(seeded_supplier_truth())


# ---------- External product providers ----------


def normalize_allergen(tag: str) -> Optional[Allergen]:
    token = tag.lower()
    tree_nuts = ["almond", "cashew", "walnut", "pistachio", "pecan", "hazelnut", "macadamia", "brazil nut", "pine nut"]
    shellfish_terms = ["shrimp", "prawn", "lobster", "crab", "crustacean"]
    fish_terms = ["salmon", "tuna", "cod", "halibut", "trout", "fish"]
    if "milk" in token or "lactose" in token:
        return Allergen.MILK
    if "egg" in token:
        return Allergen.EGG
    if "soy" in token or "soya" in token:
        return Allergen.SOY
    if "wheat" in token or "gluten" in token:
        return Allergen.WHEAT
    if "sesame" in token:
        return Allergen.SESAME
    if "peanut" in token:
        return Allergen.PEANUT
    if "tree nut" in token or any(nut in token for nut in tree_nuts):
        return Allergen.TREE_NUT
    if any(term in token for term in shellfish_terms):
        return Allergen.SHELLFISH
    if any(term in token for term in fish_terms):
        return Allergen.FISH
    return None


class ProductDataProvider:
    name: ProductSource

    def lookup_by_upc(self, upc: str) -> Optional[ExternalProductRecord]:
        raise NotImplementedError


class USDAFdcProvider(ProductDataProvider):
    name = ProductSource.USDA_FDC

    def lookup_by_upc(self, upc: str) -> Optional[ExternalProductRecord]:
        api_key = os.getenv("USDA_FDC_API_KEY", "DEMO_KEY")
        headers = {"Content-Type": "application/json"}
        search_payload = json.dumps({"query": upc, "dataType": ["Branded"], "pageSize": 5}).encode("utf-8")
        search_req = urllib.request.Request(
            f"https://api.nal.usda.gov/fdc/v1/foods/search?api_key={api_key}",
            data=search_payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(search_req, timeout=config.request_timeout_seconds) as resp:
                search_data = json.load(resp)
        except Exception as exc:
            print(f"USDA FDC search failed for {upc}: {exc}")
            return None

        foods = search_data.get("foods") or []
        if not foods:
            return None
        best = None
        for f in foods:
            if str(f.get("gtinUpc", "")) == upc:
                best = f
                break
        if not best:
            best = foods[0]
        fdc_id = best.get("fdcId")
        if not fdc_id:
            return None

        detail_req = urllib.request.Request(
            f"https://api.nal.usda.gov/fdc/v1/food/{fdc_id}?api_key={api_key}",
            method="GET",
        )
        try:
            with urllib.request.urlopen(detail_req, timeout=config.request_timeout_seconds) as resp:
                detail = json.load(resp)
        except Exception as exc:
            print(f"USDA FDC detail failed for {upc}: {exc}")
            return None

        ingredients_text = detail.get("ingredients")
        allergens_raw = detail.get("allergenInfo")
        allergens_detected: List[Allergen] = []
        heuristics_used = False
        heuristics_notes = None
        if allergens_raw:
            for token in str(allergens_raw).split(","):
                allergen = normalize_allergen(token)
                if allergen:
                    allergens_detected.append(allergen)
        elif ingredients_text:
            heuristics_used = True
            heuristics_notes = "Parsed allergens heuristically from ingredients text"
            for token in str(ingredients_text).split(","):
                allergen = normalize_allergen(token)
                if allergen and allergen not in allergens_detected:
                    allergens_detected.append(allergen)

        fetched_at = utcnow()
        return ExternalProductRecord(
            upc=upc,
            name=detail.get("description") or best.get("description"),
            brand_owner=detail.get("brandOwner"),
            brand=detail.get("brandName"),
            ingredients_text=ingredients_text,
            allergens_detected=allergens_detected,
            raw_allergens_text=str(allergens_raw) if allergens_raw else None,
            raw_allergens_tags=None,
            source=self.name,
            fetched_at=fetched_at,
            upstream_updated_at=None,
            heuristics_used=heuristics_used,
            heuristics_notes=heuristics_notes,
        )


class OpenFoodFactsProvider(ProductDataProvider):
    name = ProductSource.OPENFOODFACTS

    def lookup_by_upc(self, upc: str) -> Optional[ExternalProductRecord]:
        ua = os.getenv("OFF_USER_AGENT", "GardeRailMVP/0.1 (dev@example.com)")
        req = urllib.request.Request(
            f"https://world.openfoodfacts.org/api/v2/product/{upc}.json",
            headers={"User-Agent": ua},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=config.request_timeout_seconds) as resp:
                data = json.load(resp)
        except Exception as exc:
            print(f"OpenFoodFacts lookup failed for {upc}: {exc}")
            return None

        if data.get("status") != 1:
            return None
        product = data.get("product") or {}
        ingredients_text = product.get("ingredients_text")
        allergens_tags = product.get("allergens_tags")
        allergens_raw = product.get("allergens")
        allergens_detected: List[Allergen] = []
        heuristics_used = False
        heuristics_notes = None

        if allergens_tags:
            for token in allergens_tags:
                allergen = normalize_allergen(token)
                if allergen:
                    allergens_detected.append(allergen)
        elif allergens_raw:
            for token in str(allergens_raw).split(","):
                allergen = normalize_allergen(token)
                if allergen:
                    allergens_detected.append(allergen)

        if not allergens_detected and ingredients_text:
            heuristics_used = True
            heuristics_notes = "Parsed allergens heuristically from ingredients text"
            for token in str(ingredients_text).split(","):
                allergen = normalize_allergen(token)
                if allergen and allergen not in allergens_detected:
                    allergens_detected.append(allergen)

        fetched_at = utcnow()
        upstream_updated = None
        if product.get("last_modified_t"):
            upstream_updated = datetime.fromtimestamp(product["last_modified_t"], tz=timezone.utc)

        return ExternalProductRecord(
            upc=upc,
            name=product.get("product_name"),
            brand_owner=product.get("brands"),
            brand=product.get("brands"),
            ingredients_text=ingredients_text,
            allergens_detected=allergens_detected,
            raw_allergens_text=allergens_raw,
            raw_allergens_tags=allergens_tags,
            source=self.name,
            fetched_at=fetched_at,
            upstream_updated_at=upstream_updated,
            heuristics_used=heuristics_used,
            heuristics_notes=heuristics_notes,
        )


class SmartLabelProvider(ProductDataProvider):
    name = ProductSource.SMARTLABEL

    def lookup_by_upc(self, upc: str) -> Optional[ExternalProductRecord]:
        # TODO: Implement once LabelInsight SmartLabel credentials (X-API-KEY and org-key) are available.
        return None


providers: List[ProductDataProvider] = [
    USDAFdcProvider(),
    OpenFoodFactsProvider(),
    SmartLabelProvider(),
]


def _cache_valid(entry: Dict[str, object]) -> bool:
    return entry.get("expires_at") and entry["expires_at"] > utcnow()


def cache_external_record(upc: str, record: ExternalProductRecord):
    provider_cache_by_upc[upc] = {
        "record": record,
        "expires_at": utcnow() + timedelta(hours=config.provider_cache_ttl_hours),
    }


def cache_sku_product(sku: str, product: DistributorProduct):
    provider_cache_by_sku[sku] = {
        "product": product,
        "expires_at": utcnow() + timedelta(hours=config.provider_cache_ttl_hours),
    }


def lookup_external_by_upc(upc: str) -> Optional[ExternalProductRecord]:
    cached = provider_cache_by_upc.get(upc)
    if cached and _cache_valid(cached):
        return cached["record"]

    for provider in providers:
        record = provider.lookup_by_upc(upc)
        if record:
            cache_external_record(upc, record)
            return record
    return None


def external_to_distributor(record: ExternalProductRecord, sku: str, existing: Optional[DistributorProduct] = None) -> DistributorProduct:
    now = utcnow()
    allergens: Dict[Allergen, AllergenState] = {}
    detected = set(record.allergens_detected)
    for allergen in Allergen:
        if allergen in detected:
            allergens[allergen] = AllergenState.CONTAINS
        else:
            allergens[allergen] = AllergenState.UNKNOWN

    return DistributorProduct(
        sku=sku,
        brand=record.brand or record.brand_owner or (existing.brand if existing else ""),
        name=record.name or (existing.name if existing else ""),
        upc=record.upc,
        allergens=allergens,
        version=(existing.version if existing else "external-1.0.0"),
        updated_at=record.fetched_at,
        fetched_at=record.fetched_at,
        upstream_updated_at=record.upstream_updated_at,
        source=record.source,
    )


def get_product_truth(sku: str) -> Optional[DistributorProduct]:
    cached = provider_cache_by_sku.get(sku)
    if cached and _cache_valid(cached):
        return cached["product"]

    base_product = distributor_catalog.get(sku)
    upc = base_product.upc if base_product and base_product.upc else None
    if not upc and sku.isdigit():
        upc = sku

    product_from_provider: Optional[DistributorProduct] = None
    if upc:
        record = lookup_external_by_upc(upc)
        if record:
            product_from_provider = external_to_distributor(record, sku, existing=base_product)

    result: Optional[DistributorProduct] = product_from_provider or base_product
    if result:
        cache_sku_product(sku, result)
    return result


def lookup_supplier_truth(gtin: str, lot: str) -> Optional[SupplierProductTruth]:
    return supplier_truth_catalog.get((gtin, lot))


# Avoid broad substring matches (e.g., "nut" matches coconut/donut/nutrient); keep mappings explicit.
def risk_flags_to_may_contain(truth: SupplierProductTruth) -> Dict[Allergen, AllergenState]:
    mapping: Dict[Allergen, AllergenState] = {}
    for flag in truth.risk_flags:
        flag_lower = flag.lower()
        if "sesame" in flag_lower:
            mapping[Allergen.SESAME] = AllergenState.MAY_CONTAIN
        if "soy" in flag_lower:
            mapping[Allergen.SOY] = AllergenState.MAY_CONTAIN
        if "peanut" in flag_lower:
            mapping[Allergen.PEANUT] = AllergenState.MAY_CONTAIN
        if "tree_nut" in flag_lower or "tree nut" in flag_lower or "tree_nuts" in flag_lower or "tree nuts" in flag_lower:
            mapping[Allergen.TREE_NUT] = AllergenState.MAY_CONTAIN
        if "wheat" in flag_lower or "gluten" in flag_lower:
            mapping[Allergen.WHEAT] = AllergenState.MAY_CONTAIN
        if "milk" in flag_lower or "dairy" in flag_lower:
            mapping[Allergen.MILK] = AllergenState.MAY_CONTAIN
        if "egg" in flag_lower:
            mapping[Allergen.EGG] = AllergenState.MAY_CONTAIN
        if "fish" in flag_lower:
            mapping[Allergen.FISH] = AllergenState.MAY_CONTAIN
        if "shellfish" in flag_lower or "crustacean" in flag_lower:
            mapping[Allergen.SHELLFISH] = AllergenState.MAY_CONTAIN
    return mapping


def get_allergen_profile_for_truth(
    truth: SupplierProductTruth,
) -> Tuple[Dict[Allergen, AllergenState], str, Optional[datetime], Optional[datetime]]:
    """
    Determine allergen profile for supplier truth, optionally enriched via public UPC providers.
    Returns (allergen_profile, provider_name, provider_fetched_at, data_updated_at)
    """
    if truth.allergens:
        profile = dict(truth.allergens)
        for allergen, state in risk_flags_to_may_contain(truth).items():
            if profile.get(allergen) in (AllergenState.CONTAINS, AllergenState.NOT_PRESENT):
                continue
            profile[allergen] = state
        return profile, "SUPPLIER_TRUTH", None, truth.updated_at

    if truth.upc:
        record = lookup_external_by_upc(truth.upc)
        if record:
            profile: Dict[Allergen, AllergenState] = {}
            for allergen in Allergen:
                if allergen in record.allergens_detected:
                    profile[allergen] = AllergenState.CONTAINS
                else:
                    profile[allergen] = AllergenState.UNKNOWN
            updated_at = record.upstream_updated_at or record.fetched_at
            return profile, record.source.value, record.fetched_at, updated_at

    return {}, "NONE", None, None


def link_active_product(location_id: str, base_ingredient_id: str, gtin: str, lot: str) -> ActiveProduct:
    truth = lookup_supplier_truth(gtin, lot)
    if not truth:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Supplier truth not found for GTIN {gtin} lot {lot}",
        )
    linked_at = utcnow()
    active_products[(location_id, base_ingredient_id)] = {
        "gtin": gtin,
        "lot": lot,
        "linked_at": linked_at,
    }
    return ActiveProduct(
        base_ingredient_id=base_ingredient_id,
        gtin=gtin,
        lot=lot,
        brand=truth.brand,
        name=truth.name,
        upc=truth.upc,
        version=truth.version,
        updated_at=truth.updated_at,
        recall=truth.recall,
        risk_flags=truth.risk_flags,
        linked_at=linked_at,
    )


def get_active_product(location_id: str, base_ingredient_id: str) -> Optional[Dict[str, object]]:
    return active_products.get((location_id, base_ingredient_id))


def list_active_products(location_id: str) -> List[ActiveProduct]:
    items: List[ActiveProduct] = []
    for (loc, base_id), mapping in active_products.items():
        if loc != location_id:
            continue
        truth = lookup_supplier_truth(mapping["gtin"], mapping["lot"])
        if not truth:
            continue
        items.append(
            ActiveProduct(
                base_ingredient_id=base_id,
                gtin=truth.gtin,
                lot=truth.lot,
                brand=truth.brand,
                name=truth.name,
                upc=truth.upc,
                version=truth.version,
                updated_at=truth.updated_at,
                recall=truth.recall,
                risk_flags=truth.risk_flags,
                linked_at=mapping["linked_at"],
            )
        )
    return items


menu_items = [
    MenuItem(
        item_id="chicken_burrito",
        name="Chicken Burrito",
        modifiers=[
            MenuModifier(
                name="tortilla",
                options=[
                    MenuModifierOption(value="flour", label="Flour"),
                    MenuModifierOption(value="corn", label="Corn"),
                ],
                default="flour",
            ),
            MenuModifier(
                name="cheese",
                options=[
                    MenuModifierOption(value="yes", label="Cheese"),
                    MenuModifierOption(value="no", label="No Cheese"),
                ],
                default="yes",
            ),
            MenuModifier(
                name="sauce",
                options=[
                    MenuModifierOption(value="none", label="No Sauce"),
                    MenuModifierOption(value="chipotle_mayo", label="Chipotle Mayo"),
                ],
                default="none",
            ),
        ],
    )
]


def get_menu_item(item_id: str) -> Optional[MenuItem]:
    for item in menu_items:
        if item.item_id == item_id:
            return item
    return None


def resolve_recipe(cart_item: VerifyCartItem) -> ItemMappingTrace:
    base_ingredients = ["CHICKEN"]
    final_base_ingredients = list(base_ingredients)
    matched_rules: List[MappingRuleTrace] = []

    rules = [
        {
            "name": "tortilla_flour",
            "priority": 100,
            "condition": lambda mods: mods.get("tortilla") == "flour",
            "base_ingredients": ["TORTILLA_FLOUR"],
        },
        {
            "name": "tortilla_corn",
            "priority": 90,
            "condition": lambda mods: mods.get("tortilla") == "corn",
            "base_ingredients": ["TORTILLA_CORN"],
        },
        {
            "name": "cheese_yes",
            "priority": 80,
            "condition": lambda mods: mods.get("cheese", "yes") == "yes",
            "base_ingredients": ["CHEESE"],
        },
        {
            "name": "sauce_chipotle_mayo",
            "priority": 70,
            "condition": lambda mods: mods.get("sauce") == "chipotle_mayo",
            "base_ingredients": ["MAYO"],
        },
    ]

    for rule in sorted(rules, key=lambda r: r["priority"], reverse=True):
        matched = bool(rule["condition"](cart_item.modifiers))
        added = rule["base_ingredients"] if matched else []
        if matched:
            final_base_ingredients.extend(added)
        matched_rules.append(
            MappingRuleTrace(
                rule=rule["name"],
                matched=matched,
                added_skus=added,
                details={"priority": str(rule["priority"])}
                if matched
                else {"priority": str(rule["priority"])},
            )
        )

    return ItemMappingTrace(
        item_id=cart_item.item_id,
        base_ingredients=base_ingredients,
        matched_rules=matched_rules,
        final_base_ingredients=final_base_ingredients,
    )


def is_stale_timestamp(ts: Optional[datetime]) -> bool:
    if not ts:
        return True
    cutoff = utcnow() - timedelta(days=config.stale_days)
    return ts < cutoff


def cart_hash(payload: dict) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


audit_log: Dict[str, AuditRecord] = {}
checkout_tokens: Dict[str, dict] = {}


@app.get("/v1/distributor/products/{sku}", response_model=DistributorProduct)
def get_distributor_product(sku: str = Path(..., description="SKU identifier")):
    product = distributor_catalog.get(sku)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SKU not found")
    return product


@app.get("/v1/menu", response_model=List[MenuItem])
def get_menu():
    return menu_items


@app.get("/v1/providers")
def list_providers():
    readiness = []
    usda_key = os.getenv("USDA_FDC_API_KEY", "")
    using_demo = usda_key == "" or usda_key == "DEMO_KEY"
    readiness.append(
        {
            "name": ProductSource.USDA_FDC,
            "ready": bool(usda_key) and not using_demo,
            "using_demo_key": using_demo,
            "env": "USDA_FDC_API_KEY",
        }
    )
    readiness.append(
        {
            "name": ProductSource.OPENFOODFACTS,
            "ready": True,
            "env": "OFF_USER_AGENT (optional)",
        }
    )
    readiness.append(
        {
            "name": ProductSource.SMARTLABEL,
            "ready": bool(os.getenv("SMARTLABEL_API_KEY") and os.getenv("SMARTLABEL_ORG_KEY")),
            "env": "SMARTLABEL_API_KEY + SMARTLABEL_ORG_KEY (not implemented)",
            "note": "SmartLabel/LabelInsight adapter stub",
        }
    )
    return {"providers": readiness}


@app.get("/v1/lookup/upc/{upc}", response_model=ExternalProductRecord)
def lookup_upc(upc: str = Path(..., description="UPC/GTIN to lookup via public providers")):
    record = lookup_external_by_upc(upc)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found from providers")
    if record.source == ProductSource.OPENFOODFACTS:
        record_copy = record.copy()
        record_copy.disclaimer = "OPENFOODFACTS is community-sourced; data not guaranteed."
        return record_copy
    return record


@app.get("/v1/supplier/truth", response_model=SupplierProductTruth)
def get_supplier_truth(gtin: str, lot: str):
    truth = lookup_supplier_truth(gtin, lot)
    if not truth:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier truth not found")
    return truth


@app.post("/v1/inventory/scan", response_model=InventoryScanResponse)
def inventory_scan(request: InventoryScanRequest):
    active = link_active_product(request.location_id, request.base_ingredient_id, request.gtin, request.lot)
    truth = lookup_supplier_truth(request.gtin, request.lot)
    return InventoryScanResponse(
        ok=True,
        location_id=request.location_id,
        base_ingredient_id=request.base_ingredient_id,
        linked_at=active.linked_at,
        supplier_truth=truth,
    )


@app.get("/v1/inventory/active", response_model=InventoryActiveResponse)
def inventory_active(location_id: str):
    return InventoryActiveResponse(location_id=location_id, active=list_active_products(location_id))


def summarize(decision: VerificationDecision, reasons: List[Reason]) -> str:
    if decision == VerificationDecision.SAFE:
        return "SAFE — this dish does not contain declared allergens."
    if decision == VerificationDecision.UNSAFE and reasons:
        return f"UNSAFE — {reasons[0].explanation}"
    return "UNKNOWN — cannot guarantee safety due to stale or missing ingredient data."


@app.post("/v1/verify", response_model=VerifyResponse)
def verify(request: VerifyRequest):
    if not request.cart:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cart is empty")

    reasons: List[Reason] = []
    mapping_traces: List[ItemMappingTrace] = []
    ingredient_versions: Dict[str, str] = {}
    base_ingredients_used: Dict[str, List[str]] = {}
    active_product_links: Dict[str, Dict[str, str]] = {}

    decision = VerificationDecision.SAFE

    for item in request.cart:
        menu_item = get_menu_item(item.item_id)
        if not menu_item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown menu item {item.item_id}")

        trace = resolve_recipe(item)
        mapping_traces.append(trace)
        base_ingredients_used[item.item_id] = trace.final_base_ingredients
        active_product_links[item.item_id] = {}

        for base_id in trace.final_base_ingredients:
            active = get_active_product(request.location_id, base_id)
            if not active:
                if decision != VerificationDecision.UNSAFE:
                    decision = VerificationDecision.UNKNOWN
                reasons.append(
                    Reason(
                        allergen=request.allergies[0] if request.allergies else Allergen.WHEAT,
                        ingredient_sku=base_id,
                        ingredient_name=base_id,
                        state=AllergenState.UNKNOWN,
                        explanation=f"Unknown — no active scanned product for base ingredient {base_id} at this location.",
                    )
                )
                continue

            truth = lookup_supplier_truth(active["gtin"], active["lot"])
            if not truth:
                if decision != VerificationDecision.UNSAFE:
                    decision = VerificationDecision.UNKNOWN
                reasons.append(
                    Reason(
                        allergen=request.allergies[0] if request.allergies else Allergen.WHEAT,
                        ingredient_sku=base_id,
                        ingredient_name=base_id,
                        state=AllergenState.UNKNOWN,
                        explanation=f"Unknown — active product GTIN {active['gtin']} lot {active['lot']} missing supplier truth.",
                    )
                )
                continue

            allergen_profile, provider_name, provider_fetched_at, data_updated_at = get_allergen_profile_for_truth(truth)

            ingredient_versions[base_id] = (
                f"gtin={truth.gtin}, lot={truth.lot}, truth_version={truth.version}, "
                f"provider={provider_name}, provider_updated_at={data_updated_at}"
            )
            active_product_links[item.item_id][base_id] = f"gtin={truth.gtin}, lot={truth.lot}"

            if not allergen_profile:
                if decision != VerificationDecision.UNSAFE:
                    decision = VerificationDecision.UNKNOWN
                reasons.append(
                    Reason(
                        allergen=request.allergies[0] if request.allergies else Allergen.WHEAT,
                        ingredient_sku=truth.gtin,
                        ingredient_name=truth.name,
                        state=AllergenState.UNKNOWN,
                        explanation=f"Unknown — no allergen data for base ingredient {base_id} lot {truth.lot} and no UPC enrichment available.",
                        provider=str(provider_name),
                        provider_fetched_at=provider_fetched_at,
                    )
                )
                continue

            if is_stale_timestamp(data_updated_at):
                if decision != VerificationDecision.UNSAFE:
                    decision = VerificationDecision.UNKNOWN
                stale_expl = (
                    "Unknown — provider data for UPC enrichment is stale."
                    if provider_name != "SUPPLIER_TRUTH"
                    else f"Unknown — supplier truth for {truth.gtin} lot {truth.lot} is stale."
                )
                reasons.append(
                    Reason(
                        allergen=request.allergies[0] if request.allergies else Allergen.WHEAT,
                        ingredient_sku=truth.gtin,
                        ingredient_name=truth.name,
                        state=AllergenState.UNKNOWN,
                        explanation=stale_expl,
                        provider=str(provider_name),
                        provider_fetched_at=provider_fetched_at,
                    )
                )
                continue

            if truth.recall:
                decision = VerificationDecision.UNSAFE
                reasons.append(
                    Reason(
                        allergen=request.allergies[0] if request.allergies else Allergen.WHEAT,
                        ingredient_sku=truth.gtin,
                        ingredient_name=truth.name,
                        state=AllergenState.CONTAINS,
                        explanation=f"Unsafe — product lot {truth.lot} (GTIN {truth.gtin}) is under recall.",
                        provider=str(provider_name),
                        provider_fetched_at=provider_fetched_at,
                    )
                )
                continue

            for allergen in request.allergies:
                state = allergen_profile.get(allergen, AllergenState.UNKNOWN)
                if state == AllergenState.CONTAINS:
                    decision = VerificationDecision.UNSAFE
                    reasons.append(
                        Reason(
                            allergen=allergen,
                            ingredient_sku=truth.gtin,
                            ingredient_name=truth.name,
                            state=state,
                            explanation=f"Unsafe — base ingredient {base_id} lot {truth.lot} (GTIN {truth.gtin}) contains {allergen}.",
                            provider=str(provider_name),
                            provider_fetched_at=provider_fetched_at,
                        )
                    )
                elif state == AllergenState.MAY_CONTAIN:
                    if decision == VerificationDecision.UNSAFE:
                        continue
                    if config.may_contain_unsafe:
                        decision = VerificationDecision.UNSAFE
                        reasons.append(
                            Reason(
                                allergen=allergen,
                                ingredient_sku=truth.gtin,
                                ingredient_name=truth.name,
                                state=state,
                                explanation=f"Unsafe — policy marks MAY_CONTAIN as unsafe for {base_id}.",
                                provider=str(provider_name),
                                provider_fetched_at=provider_fetched_at,
                            )
                        )
                    else:
                        if decision == VerificationDecision.SAFE:
                            decision = VerificationDecision.UNKNOWN
                        reasons.append(
                            Reason(
                                allergen=allergen,
                                ingredient_sku=truth.gtin,
                                ingredient_name=truth.name,
                                state=state,
                                explanation=f"Unknown — {base_id} lot {truth.lot} may contain {allergen}; policy allows but cannot guarantee.",
                                provider=str(provider_name),
                                provider_fetched_at=provider_fetched_at,
                            )
                        )
                elif state == AllergenState.UNKNOWN:
                    if decision == VerificationDecision.UNSAFE:
                        continue
                    if decision == VerificationDecision.SAFE:
                        decision = VerificationDecision.UNKNOWN
                    reasons.append(
                        Reason(
                            allergen=allergen,
                            ingredient_sku=truth.gtin,
                            ingredient_name=truth.name,
                            state=state,
                            explanation=f"Unknown — allergen presence unknown for base ingredient {base_id} lot {truth.lot}.",
                            provider=str(provider_name),
                            provider_fetched_at=provider_fetched_at,
                        )
                    )

    if decision == VerificationDecision.SAFE:
        reasons = []

    audit_id = str(uuid.uuid4())
    token = str(uuid.uuid4())
    token_expires_at = utcnow() + timedelta(minutes=config.checkout_token_ttl_minutes)

    audit_record = AuditRecord(
        audit_id=audit_id,
        timestamp=utcnow(),
        location_id=request.location_id,
        request_payload=json.loads(request.json()),
        decision=decision,
        reasons=reasons,
        mapping_trace=mapping_traces,
        ingredient_versions_used=ingredient_versions,
        base_ingredients_used=base_ingredients_used,
        active_product_links=active_product_links,
        cart_hash=cart_hash(json.loads(request.json())),
        policy={
            "stale_days": str(config.stale_days),
            "may_contain_unsafe": str(config.may_contain_unsafe),
        },
    )
    audit_log[audit_id] = audit_record

    checkout_tokens[token] = {
        "audit_id": audit_id,
        "decision": decision,
        "expires_at": token_expires_at,
        "consented_at": None,
    }

    summary = summarize(decision, reasons)

    return VerifyResponse(
        decision=decision,
        summary=summary,
        reasons=reasons,
        audit_id=audit_id,
        checkout_token=token,
        token_expires_at=token_expires_at,
    )


@app.post("/v1/consent", response_model=ConsentResponse)
def consent(request: ConsentRequest):
    token_record = checkout_tokens.get(request.checkout_token)
    if not token_record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Checkout token not found")
    if token_record["audit_id"] != request.audit_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Audit mismatch for token")
    if utcnow() > token_record["expires_at"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Checkout token expired")
    if not request.accepted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Consent not provided")

    consented_at = utcnow()
    token_record["consented_at"] = consented_at
    audit_record = audit_log.get(request.audit_id)
    if audit_record:
        updated = audit_record.dict()
        updated["consent_timestamp"] = consented_at
        audit_log[request.audit_id] = AuditRecord(**updated)

    return ConsentResponse(ok=True, consented_at=consented_at, audit_id=request.audit_id)


@app.post("/v1/checkout", response_model=CheckoutResponse)
def checkout(request: CheckoutRequest):
    token_record = checkout_tokens.get(request.checkout_token)
    if not token_record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Checkout token not found")
    if utcnow() > token_record["expires_at"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Checkout token expired")
    if not token_record.get("consented_at"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Consent required before checkout")

    return CheckoutResponse(ok=True, audit_id=token_record["audit_id"], decision=token_record["decision"])


@app.get("/v1/audit/{audit_id}", response_model=AuditResponse)
def get_audit(audit_id: str):
    record = audit_log.get(audit_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit not found")
    return AuditResponse(audit=record)


@app.get("/v1/audit")
def audit_index():
    latest = None
    if audit_log:
        latest = sorted(audit_log.values(), key=lambda r: r.timestamp, reverse=True)[0].audit_id
    return JSONResponse({"count": len(audit_log), "latest_audit_id": latest})
