import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class TaskCreate(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        cleaned = value.strip()
        if _CONTROL.search(cleaned):
            raise ValueError("Название содержит недопустимые символы.")
        if not cleaned:
            raise ValueError("Введите название задачи.")
        if len(cleaned) > 200:
            raise ValueError("Название не длиннее 200 символов.")
        return cleaned


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    done: bool
    created_at: datetime
