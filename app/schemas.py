import re
import unicodedata
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def normalize_query(value: str) -> str:
    value = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not value or len(value) > 120 or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("Поисковая фраза должна содержать от 1 до 120 обычных символов")
    return value


class ProductData(BaseModel):
    external_id: str
    url: str
    title: str = Field(min_length=1, max_length=500)
    price: Decimal = Field(ge=0)
    condition: str | None = None
    description: str = ""
    images: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    category_path: str
    published_at: datetime | None = None


class ListingPage(BaseModel):
    products: list[ProductData]
    total: int = Field(ge=0)
    next_cursor: str | None = None
    newest_first: bool = True


class CategoryData(BaseModel):
    name: str
    path: str
    parent_path: str | None = None


class RegisterUser(BaseModel):
    telegram_id: int = Field(gt=0)
    chat_id: int = Field(gt=0)
    username: str | None = Field(None, max_length=64)


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    telegram_id: int
    chat_id: int
    active: bool


class UserUpdate(BaseModel):
    active: bool


class KeywordInput(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return normalize_query(value)


class CategoryInput(BaseModel):
    category_id: int = Field(gt=0)


class DeliveryUpdate(BaseModel):
    lease_token: str = Field(min_length=1, max_length=64)
    status: str = Field(pattern=r"^(sent|retry|uncertain|failed|blocked)$")
    message_ids: list[int] = Field(default_factory=list, max_length=10)
    photo_file_ids: list[str] = Field(default_factory=list, max_length=10)
    retry_after: int = Field(60, ge=1, le=86400)
    error: str | None = Field(None, max_length=200)


class PhotoReference(BaseModel):
    image_url: str = Field(max_length=1500)
    file_id: str | None = Field(None, min_length=1, max_length=2048)


class PhotoReferences(BaseModel):
    photos: list[PhotoReference] = Field(max_length=10)


CONDITIONS = ("новый", "отличное", "хорошее", "удовлетворительное")


class SubscriptionFilters(BaseModel):
    price_min: Decimal | None = Field(None, ge=0, max_digits=14, decimal_places=2)
    price_max: Decimal | None = Field(None, ge=0, max_digits=14, decimal_places=2)
    stop_words: list[str] = Field(default_factory=list, max_length=20)
    conditions: list[str] = Field(default_factory=list, max_length=4)
    cities: list[str] = Field(default_factory=list, max_length=40)

    @field_validator("stop_words", "conditions", "cities")
    @classmethod
    def clean_values(cls, values):
        return list(dict.fromkeys(normalize_query(value) for value in values))

    @model_validator(mode="after")
    def valid_ranges(self):
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError("Минимальная цена не может быть выше максимальной")
        if any(value not in CONDITIONS for value in self.conditions):
            raise ValueError("Неизвестное состояние товара")
        return self


class TimedMode(BaseModel):
    minutes: int = Field(15, ge=5, le=60)


class IntervalOverride(TimedMode):
    seconds: int = Field(300, ge=300, le=86400)


def validate_cursor(value: str | None) -> str | None:
    if value is not None and (len(value) > 2048 or not re.fullmatch(r"[A-Za-z0-9_+/=-]+", value)):
        raise ValueError("Invalid pagination cursor")
    return value
