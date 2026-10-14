"""Pydantic v2 request/response models for the API surface."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str


class QuotaOut(BaseModel):
    limit: int
    used: int
    remaining: int
    cost: int
    messages_left: int
    blocked_until: datetime | None
    retry_after_seconds: int
    block_seconds: int
    server_time: datetime


class MeOut(BaseModel):
    user_id: uuid.UUID
    username: str
    role: Literal["admin", "user"]
    quota: QuotaOut | None  # None for admins (unlimited)


class UserOut(BaseModel):
    user_id: uuid.UUID
    username: str
    role: Literal["admin", "user"]
    is_active: bool
    credit_limit: int
    quota: QuotaOut
    created_at: datetime | None
    last_login_at: datetime | None
    managed_by_env: bool = False  # the .env admin: its credentials can only change in .env


USERNAME_PATTERN = r"^[A-Za-z0-9_.-]{3,32}$"


class UserCreate(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=10, max_length=128)
    role: Literal["admin", "user"] = "user"
    credit_limit: int | None = Field(default=None, ge=1, le=1_000_000)


class UserUpdate(BaseModel):
    username: str | None = Field(default=None, pattern=USERNAME_PATTERN)
    password: str | None = Field(default=None, min_length=10, max_length=128)
    role: Literal["admin", "user"] | None = None
    is_active: bool | None = None
    credit_limit: int | None = Field(default=None, ge=1, le=1_000_000)


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    document_id: uuid.UUID
    filename: str
    doc_type: str
    is_scanned: bool
    page_count: int | None
    status: str
    status_detail: str | None
    error: str | None
    created_at: datetime | None
    folder_ids: list[uuid.UUID] = []


class FolderOut(BaseModel):
    folder_id: uuid.UUID
    name: str
    created_at: datetime | None
    document_count: int = 0


class FolderCreate(BaseModel):
    name: str


class FolderRename(BaseModel):
    name: str


class SetDocumentFoldersRequest(BaseModel):
    folder_ids: list[uuid.UUID]


class UploadResponse(BaseModel):
    document_id: uuid.UUID
    status: str


class TreeNode(BaseModel):
    title: str
    node_id: str | None = None
    page_number: int | None = None
    summary: str | None = None
    prefix_summary: str | None = None
    nodes: list["TreeNode"] = []


TreeNode.model_rebuild()


class DocumentTreeResponse(BaseModel):
    document_id: uuid.UUID
    tree: list[TreeNode]


class Citation(BaseModel):
    document_id: uuid.UUID | None = None
    document_name: str
    page: int | None = None
    figure_id: str | None = None
    image_url: str | None = None
    source: str  # "tree" | "diagram"


class ChatHistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    document_ids: list[uuid.UUID] | None = None  # None = all indexed documents
    # Prior turns of this session, oldest first -- NOT including `query`
    # itself. The vendored engine's chat() takes a full role/content list
    # for multi-turn ("keep your own role/content list of the visible
    # conversation... and pass it back" -- rag_core/client.py's chat()
    # docstring); without this, every query was answered with zero memory
    # of the conversation despite the UI showing full history.
    history: list[ChatHistoryMessage] = []
