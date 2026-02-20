"""Pydantic модели для заказов."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Order(BaseModel):
    """Модель заказа."""

    model_config = {"protected_namespaces": ()}

    id: Optional[int] = None
    model: str = ""
    price: str = ""
    address: str = ""
    contact_raw: str = ""
    phone: Optional[str] = None
    customer_name: str = ""
    comment: str = ""
    manager_id: int
    manager_name: str = ""
    chat_id: int
    message_id: int
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    status: str = "new"
