"""Internal Track 2 listing extraction contract.

These observations are model claims requiring human review, not verified image facts.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from schemas.dto import UUIDText


MAX_LISTING_IMAGE_BYTES = 5 * 1024 * 1024
MAX_LISTING_BASE64_LENGTH = 4 * ((MAX_LISTING_IMAGE_BYTES + 2) // 3)


class FieldState(StrEnum):
    FOUND = "FOUND"
    UNKNOWN = "UNKNOWN"
    AMBIGUOUS = "AMBIGUOUS"


class UnknownReason(StrEnum):
    ABSENT = "ABSENT"
    UNREADABLE = "UNREADABLE"
    NOT_CONFIRMED = "NOT_CONFIRMED"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class ListingContextRequest(StrictModel):
    request_id: UUIDText = Field(alias="requestId")
    evidence_id: UUIDText = Field(alias="evidenceId")
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", alias="imageSha256")
    mime_type: Literal["image/jpeg", "image/png"] = Field(alias="mimeType")
    image_base64: str = Field(min_length=1, max_length=MAX_LISTING_BASE64_LENGTH, alias="imageBase64")


def _price_from_raw(raw: str) -> Decimal | None:
    display = re.sub(r"\s+", "", raw)
    display = re.sub(r"^(?:KRW|USD|JPY|EUR|₩|\$|€|¥)", "", display, flags=re.IGNORECASE)
    display = re.sub(r"(?:KRW|USD|JPY|EUR|원|₩|\$|€|¥)$", "", display, flags=re.IGNORECASE)
    match = re.fullmatch(r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(만|천)?", display)
    if not match:
        return None
    try:
        amount = Decimal(match.group(1).replace(",", ""))
        if match.group(2) == "만":
            amount *= 10000
        elif match.group(2) == "천":
            amount *= 1000
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and 0 <= amount <= Decimal("999999999999.99") else None


class PriceValue(StrictModel):
    raw: str = Field(min_length=1, max_length=64)
    amount: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,2})?$", max_length=15)

    @model_validator(mode="after")
    def check_normalization(self):
        normalized = _price_from_raw(self.raw)
        if normalized is None or normalized != Decimal(self.amount):
            raise PydanticCustomError("price_mismatch", "Price cannot be verified from raw display")
        return self


class TextObservation(StrictModel):
    state: Literal["FOUND", "UNKNOWN", "AMBIGUOUS"]
    value: str | None = Field(default=None, max_length=256)
    evidence_quote: str | None = Field(default=None, min_length=1, max_length=512, alias="evidenceQuote")
    unknown_reason: Literal["ABSENT", "UNREADABLE", "NOT_CONFIRMED"] | None = Field(default=None, alias="unknownReason")
    candidates: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def check_state(self):
        if self.state == FieldState.FOUND:
            if not self.value or not self.value.strip() or not self.evidence_quote:
                raise PydanticCustomError("unsupported_found", "FOUND requires a value and quote")
            if self.unknown_reason is not None or self.candidates:
                raise PydanticCustomError("invalid_state", "FOUND cannot have unknown reason or candidates")
            if self.value.casefold() not in self.evidence_quote.casefold() and not (
                self.value == "KRW" and any(token in self.evidence_quote for token in ("원", "₩"))
            ):
                raise PydanticCustomError("quote_mismatch", "Value must occur in evidence quote")
        elif self.state == FieldState.UNKNOWN:
            if self.value is not None or self.evidence_quote is not None or self.unknown_reason is None or self.candidates:
                raise PydanticCustomError("invalid_state", "UNKNOWN requires null value and reason")
        else:
            if self.value is not None or not self.evidence_quote or self.unknown_reason is not None:
                raise PydanticCustomError("invalid_state", "AMBIGUOUS cannot have a decided value")
            if len(self.candidates) < 2 or len(set(self.candidates)) != len(self.candidates):
                raise PydanticCustomError("invalid_candidates", "AMBIGUOUS requires distinct candidates")
        if any(not item.strip() or len(item) > 128 for item in self.candidates):
            raise PydanticCustomError("invalid_candidates", "Candidates must be bounded text")
        return self


class PriceObservation(StrictModel):
    state: Literal["FOUND", "UNKNOWN", "AMBIGUOUS"]
    value: PriceValue | None = None
    evidence_quote: str | None = Field(default=None, min_length=1, max_length=512, alias="evidenceQuote")
    unknown_reason: Literal["ABSENT", "UNREADABLE", "NOT_CONFIRMED"] | None = Field(default=None, alias="unknownReason")
    candidates: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def check_state(self):
        if self.state == FieldState.FOUND:
            if self.value is None or not self.evidence_quote or self.value.raw not in self.evidence_quote:
                raise PydanticCustomError("quote_mismatch", "Price display must occur in evidence quote")
            if self.unknown_reason is not None or self.candidates:
                raise PydanticCustomError("invalid_state", "FOUND cannot have unknown reason or candidates")
        elif self.state == FieldState.UNKNOWN:
            if self.value is not None or self.evidence_quote is not None or self.unknown_reason is None or self.candidates:
                raise PydanticCustomError("invalid_state", "UNKNOWN requires null value and reason")
        else:
            if self.value is not None or not self.evidence_quote or self.unknown_reason is not None:
                raise PydanticCustomError("invalid_state", "AMBIGUOUS cannot have a decided value")
            if len(self.candidates) < 2 or len(set(self.candidates)) != len(self.candidates):
                raise PydanticCustomError("invalid_candidates", "AMBIGUOUS requires distinct candidates")
        if any(not item.strip() or len(item) > 128 for item in self.candidates):
            raise PydanticCustomError("invalid_candidates", "Candidates must be bounded text")
        return self


class ListingFields(StrictModel):
    event_name: TextObservation = Field(alias="eventName")
    performance_at: TextObservation = Field(alias="performanceAt")
    platform_display_name: TextObservation = Field(alias="platformDisplayName")
    asking_price: PriceObservation = Field(alias="askingPrice")
    currency: TextObservation
    sale_text: TextObservation = Field(alias="saleText")
    zone: TextObservation
    row: TextObservation
    seat: TextObservation

    @model_validator(mode="after")
    def check_field_semantics(self):
        for field in (self.zone, self.row, self.seat):
            if field.state == FieldState.FOUND:
                value = field.value or ""
                quote = field.evidence_quote or ""
                if len(value) > 64 or not re.search(r"(?<![0-9A-Za-z])" + re.escape(value) + r"(?![0-9A-Za-z])", quote):
                    raise PydanticCustomError("seat_quote_mismatch", "Seat token must occur in quote")
        if self.performance_at.state == FieldState.FOUND and len(self.performance_at.value or "") > 128:
            raise PydanticCustomError("invalid_performance_text", "Performance text is too long")
        if self.currency.state == FieldState.FOUND:
            value = self.currency.value or ""
            quote = self.currency.evidence_quote or ""
            if not re.fullmatch(r"[A-Z]{3}", value):
                raise PydanticCustomError("invalid_currency", "Currency must be a visible ISO code")
            if value == "KRW":
                supported = ("KRW", "원", "₩")
            else:
                supported = (value,)
            if not any(token.casefold() in quote.casefold() for token in supported):
                raise PydanticCustomError("currency_quote_mismatch", "Currency needs explicit quote")
        return self


def derive_listing_status(fields: ListingFields) -> Literal["COMPLETED", "PARTIAL", "UNVERIFIABLE"]:
    observations = (
        fields.event_name, fields.performance_at, fields.platform_display_name,
        fields.asking_price, fields.currency, fields.sale_text,
        fields.zone, fields.row, fields.seat,
    )
    if not any(field.state == FieldState.FOUND for field in observations):
        return "UNVERIFIABLE"
    if fields.sale_text.state == FieldState.FOUND and any(
        field.state == FieldState.FOUND
        for field in (fields.event_name, fields.platform_display_name, fields.asking_price)
    ):
        return "COMPLETED"
    return "PARTIAL"


class ListingContextResponse(StrictModel):
    request_id: UUIDText = Field(alias="requestId")
    evidence_id: UUIDText = Field(alias="evidenceId")
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", alias="imageSha256")
    status: Literal["COMPLETED", "PARTIAL", "UNVERIFIABLE"]
    fields: ListingFields
    model_name: str = Field(min_length=1, max_length=128, alias="modelName")
    model_version: str | None = Field(default=None, max_length=128, alias="modelVersion")
    analyzed_at: datetime = Field(alias="analyzedAt")
    review_required: Literal[True] = Field(default=True, alias="reviewRequired")
    error_code: None = Field(default=None, alias="errorCode")
