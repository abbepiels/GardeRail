# GardeRail FastAPI MVP
# Install/run: pip install fastapi uvicorn; uvicorn app:app --reload
# Example curls:
# Verify unsafe (wheat + flour):
# curl -X POST http://localhost:8000/v1/verify -H "Content-Type: application/json" \
#  -d '{"user_id":"u1","allergies":["WHEAT"],"cart":[{"item_id":"chicken_burrito","qty":1,"modifiers":{"tortilla":"flour","cheese":"yes","sauce":"chipotle_mayo"}}]}'
# Verify safe (wheat + corn):
# curl -X POST http://localhost:8000/v1/verify -H "Content-Type: application/json" \
#  -d '{"user_id":"u1","allergies":["WHEAT"],"cart":[{"item_id":"chicken_burrito","qty":1,"modifiers":{"tortilla":"corn","cheese":"no","sauce":"none"}}]}'
# Consent then checkout:
# TOKEN=$(curl -s -X POST http://localhost:8000/v1/verify -H "Content-Type: application/json" \
#  -d '{"user_id":"u1","allergies":["WHEAT"],"cart":[{"item_id":"chicken_burrito","qty":1,"modifiers":{"tortilla":"corn","cheese":"no","sauce":"none"}}]}' | python -c "import sys,json;print(json.load(sys.stdin)['checkout_token'])") && \
# AUDIT=$(curl -s http://localhost:8000/v1/audit | python - <<'PY'\nimport sys,json\nprint(json.load(sys.stdin)['latest_audit_id'])\nPY)
# curl -X POST http://localhost:8000/v1/consent -H "Content-Type: application/json" -d "{\"checkout_token\":\"$TOKEN\",\"audit_id\":\"$AUDIT\",\"accepted\":true}"
# curl -X POST http://localhost:8000/v1/checkout -H "Content-Type: application/json" -d "{\"checkout_token\":\"$TOKEN\"}"

from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid
from enum import Enum
from typing import Dict, List, Optional

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


class DistributorProduct(BaseModel):
    sku: str
    brand: str
    name: str
    allergens: Dict[Allergen, AllergenState]
    version: str
    updated_at: datetime


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


class MappingRuleTrace(BaseModel):
    rule: str
    matched: bool
    added_skus: List[str]
    details: Optional[Dict[str, str]] = None


class ItemMappingTrace(BaseModel):
    item_id: str
    base_skus: List[str]
    matched_rules: List[MappingRuleTrace]
    final_skus: List[str]


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


class AuditRecord(BaseModel):
    audit_id: str
    timestamp: datetime
    consent_timestamp: Optional[datetime] = None
    request_payload: dict
    decision: VerificationDecision
    reasons: List[Reason]
    mapping_trace: List[ItemMappingTrace]
    ingredient_versions_used: Dict[str, str]
    cart_hash: str
    policy: Dict[str, str]


class AuditResponse(BaseModel):
    audit: AuditRecord


class Config:
    stale_days: int = 30
    may_contain_unsafe: bool = False
    checkout_token_ttl_minutes: int = 10


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


distributor_catalog: Dict[str, DistributorProduct] = seeded_products()
config = Config()


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
    base_skus = ["CHIPOTLE_CHICKEN_PREP"]
    final_skus = list(base_skus)
    matched_rules: List[MappingRuleTrace] = []

    rules = [
        {
            "name": "tortilla_flour",
            "priority": 100,
            "condition": lambda mods: mods.get("tortilla") == "flour",
            "skus": ["MISSION_FLOUR_TORTILLA_12IN"],
        },
        {
            "name": "tortilla_corn",
            "priority": 90,
            "condition": lambda mods: mods.get("tortilla") == "corn",
            "skus": ["SUPPLIER_X_CORN_TORTILLA_6IN"],
        },
        {
            "name": "cheese_yes",
            "priority": 80,
            "condition": lambda mods: mods.get("cheese", "yes") == "yes",
            "skus": ["DAIRY_CHEESE_SHRED"],
        },
        {
            "name": "sauce_chipotle_mayo",
            "priority": 70,
            "condition": lambda mods: mods.get("sauce") == "chipotle_mayo",
            "skus": ["UNILEVER_HELLMANNS_MAYO_30OZ"],
        },
    ]

    for rule in sorted(rules, key=lambda r: r["priority"], reverse=True):
        matched = bool(rule["condition"](cart_item.modifiers))
        added = rule["skus"] if matched else []
        if matched:
            final_skus.extend(added)
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
        base_skus=base_skus,
        matched_rules=matched_rules,
        final_skus=final_skus,
    )


def is_stale(product: DistributorProduct) -> bool:
    cutoff = utcnow() - timedelta(days=config.stale_days)
    return product.updated_at < cutoff


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


def summarize(decision: VerificationDecision, reasons: List[Reason]) -> str:
    if decision == VerificationDecision.SAFE:
        return "SAFE — this dish does not contain declared allergens."
    if decision == VerificationDecision.UNSAFE and reasons:
        first = reasons[0]
        return f"UNSAFE — {first.ingredient_name} contains {first.allergen}."
    return "UNKNOWN — cannot guarantee safety due to stale or missing ingredient data."


@app.post("/v1/verify", response_model=VerifyResponse)
def verify(request: VerifyRequest):
    if not request.cart:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cart is empty")

    reasons: List[Reason] = []
    mapping_traces: List[ItemMappingTrace] = []
    ingredient_versions: Dict[str, str] = {}

    decision = VerificationDecision.SAFE

    for item in request.cart:
        menu_item = get_menu_item(item.item_id)
        if not menu_item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown menu item {item.item_id}")

        trace = resolve_recipe(item)
        mapping_traces.append(trace)

        for sku in trace.final_skus:
            product = distributor_catalog.get(sku)
            if not product:
                decision = VerificationDecision.UNKNOWN if decision != VerificationDecision.UNSAFE else decision
                reasons.append(
                    Reason(
                        allergen=request.allergies[0] if request.allergies else Allergen.WHEAT,
                        ingredient_sku=sku,
                        ingredient_name="Unknown ingredient",
                        state=AllergenState.UNKNOWN,
                        explanation="Ingredient data missing from distributor catalog",
                    )
                )
                continue

            ingredient_versions[sku] = product.version

            if is_stale(product):
                if decision != VerificationDecision.UNSAFE:
                    decision = VerificationDecision.UNKNOWN
                reasons.append(
                    Reason(
                        allergen=request.allergies[0] if request.allergies else Allergen.WHEAT,
                        ingredient_sku=product.sku,
                        ingredient_name=product.name,
                        state=AllergenState.UNKNOWN,
                        explanation="Ingredient data is stale",
                    )
                )
                continue

            for allergen in request.allergies:
                state = product.allergens.get(allergen, AllergenState.UNKNOWN)
                if state == AllergenState.CONTAINS:
                    decision = VerificationDecision.UNSAFE
                    reasons.append(
                        Reason(
                            allergen=allergen,
                            ingredient_sku=product.sku,
                            ingredient_name=product.name,
                            state=state,
                            explanation="Ingredient explicitly contains allergen",
                        )
                    )
                elif state == AllergenState.MAY_CONTAIN:
                    if config.may_contain_unsafe:
                        decision = VerificationDecision.UNSAFE
                        reasons.append(
                            Reason(
                                allergen=allergen,
                                ingredient_sku=product.sku,
                                ingredient_name=product.name,
                                state=state,
                                explanation="Policy marks MAY_CONTAIN as unsafe",
                            )
                        )
                    else:
                        if decision == VerificationDecision.SAFE:
                            decision = VerificationDecision.UNKNOWN
                        reasons.append(
                            Reason(
                                allergen=allergen,
                                ingredient_sku=product.sku,
                                ingredient_name=product.name,
                                state=state,
                                explanation="Ingredient may contain allergen; policy allows but cannot guarantee",
                            )
                        )
                elif state == AllergenState.UNKNOWN:
                    if decision == VerificationDecision.SAFE:
                        decision = VerificationDecision.UNKNOWN
                    reasons.append(
                        Reason(
                            allergen=allergen,
                            ingredient_sku=product.sku,
                            ingredient_name=product.name,
                            state=state,
                            explanation="Allergen presence unknown for this ingredient",
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
        request_payload=json.loads(request.json()),
        decision=decision,
        reasons=reasons,
        mapping_trace=mapping_traces,
        ingredient_versions_used=ingredient_versions,
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
