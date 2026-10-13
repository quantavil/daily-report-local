"""Shared data contracts. Values and their provenance travel together."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DocumentCode = Literal['SB21', 'SB22', 'SB35']
FieldState = Literal['accepted', 'blank', 'rejected', 'missing', 'error']


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class BillIdentity(Model):
    sb_no: str = Field(pattern=r'^\d{7}$')
    sb_date: date | None = None
    port_code: str | None = None
    document_date: date | None = None

    @field_validator('port_code', mode='before')
    @classmethod
    def normalize_port(cls, value):
        if value is None or not str(value).strip():
            return None
        port = str(value).strip().upper()
        if len(port) != 6 or not port.isascii() or not port.isalnum():
            raise ValueError('Port code must contain six ASCII letters/digits')
        return port


class DocumentMetadata(Model):
    identity: BillIdentity
    code: DocumentCode
    document_at: datetime
    filename: str


class Attachment(Model):
    metadata: DocumentMetadata
    message_id: str
    attachment_id: str | None = None
    inline_data: str | None = None
    part_id: str | None = None

    @property
    def key(self) -> str:
        return '|'.join((self.message_id, self.part_id or self.attachment_id or '', self.metadata.filename))


class Evidence(Model):
    filename: str
    content_hash: str
    page_number: int = Field(ge=1)
    label: str
    quote: str
    bbox: tuple[float, float, float, float]


class FieldResult(Model):
    state: FieldState
    value: str | date | list[str | None] | None = None
    value_type: Literal['text', 'date', 'sequence'] = 'text'
    evidence: list[Evidence] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode='before')
    @classmethod
    def restore_value_type(cls, data):
        if isinstance(data, dict) and data.get('value_type') == 'date' and isinstance(data.get('value'), str):
            data = dict(data)
            data['value'] = date.fromisoformat(data['value'])
        return data

    @model_validator(mode='after')
    def validate_accepted(self):
        if self.state == 'accepted' and (self.value is None or not self.evidence):
            raise ValueError('Accepted values require a value and source evidence')
        if self.state != 'accepted' and self.value is not None:
            raise ValueError('Unaccepted results cannot carry writable values')
        self.value_type = 'date' if isinstance(self.value, date) else 'sequence' if isinstance(self.value, list) else 'text'
        return self


class ContainerRecord(Model):
    number: str | None = None
    size: str | None = None
    seal: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class LayoutSpan(Model):
    text: str
    bbox: tuple[float, float, float, float]


class LayoutPage(Model):
    number: int = Field(ge=1)
    width: float
    height: float
    spans: list[LayoutSpan] = Field(default_factory=list)


class DocumentLayout(Model):
    document_code: DocumentCode | None = None
    pages: list[LayoutPage] = Field(default_factory=list)


class ParsedDocument(Model):
    metadata: DocumentMetadata
    content_hash: str
    fields: dict[str, FieldResult] = Field(default_factory=dict)
    containers: list[ContainerRecord] = Field(default_factory=list)


class BillResult(Model):
    identity: BillIdentity
    fields: dict[str, FieldResult] = Field(default_factory=dict)
    containers: list[ContainerRecord] = Field(default_factory=list)
    selected_sources: list[DocumentMetadata] = Field(default_factory=list)
    selected_hashes: dict[str, str] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class WorkbookRow(Model):
    row_idx: int = Field(ge=2)
    identity: BillIdentity
    values: dict[str, str | date | None] = Field(default_factory=dict)
    cancelled: bool = False
    error: str | None = None


class RowUpdate(Model):
    row_idx: int = Field(ge=2)
    values: dict[str, str | date | list[str | None] | None] = Field(default_factory=dict)
    containers: list[ContainerRecord] | None = None
    status: Literal['Success', 'Partial', 'Cancelled', 'Not found', 'Error']
    detail: str = ''
