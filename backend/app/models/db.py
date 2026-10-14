"""
SQLAlchemy 2.0 models. Postgres holds metadata and diagram OCR/embedding
records; the tree engine's own storage path (app/rag_service.py) holds the
actual tree/page content -- documents.rag_doc_id is the pointer between
the two.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


_DOC_TYPES = ("pdf", "docx", "csv", "xlsx", "eml", "txt", "json", "xer", "py", "md", "other")
_DOC_STATUSES = ("queued", "extracting", "ocr", "indexing", "indexed", "failed")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(f"doc_type IN {_DOC_TYPES!r}", name="documents_doc_type_check"),
        CheckConstraint(f"status IN {_DOC_STATUSES!r}", name="documents_status_check"),
        Index("documents_created_at_idx", text("created_at DESC")),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)  # pdf | docx | csv | xlsx | eml | txt | json | xer
    content_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)  # original file in object storage
    # For DOCX: the rendered PDF actually fed to the tree engine (see
    # ingestion/docx_ingest.py) -- persisted so /preview can show exactly
    # what was chunked, since a raw .docx can't be rendered by the
    # frontend's PDF viewer. Null for PDFs, which preview storage_key directly.
    preview_storage_key: Mapped[str | None] = mapped_column(Text)

    rag_doc_id: Mapped[str | None] = mapped_column(Text)  # tree engine's own doc id, once indexed
    is_scanned: Mapped[bool] = mapped_column(default=False)
    page_count: Mapped[int | None] = mapped_column()

    status: Mapped[str] = mapped_column(Text, default="queued")
    # queued | extracting | ocr | indexing | indexed | failed
    status_detail: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    # 'tree' = full section tree; 'lite' = pages + chunk index only (see app/scale/). Migration 0012.
    tier: Mapped[str] = mapped_column(Text, nullable=False, default="tree", server_default="tree")
    # Shared ingestion state (migration 0017): which replica is working on this document, and its last progress event.
    ingest_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress: Mapped[dict | None] = mapped_column(JSONB)


class DiagramPage(Base):
    """One row per scanned/image page: OCR text, VLM caption+description,
    and the pointer to its Qdrant vector (see app/retrieval/vector_store.py)."""

    __tablename__ = "diagram_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.document_id", ondelete="CASCADE")
    )
    page_number: Mapped[int] = mapped_column(nullable=False)

    ocr_text: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    figure_id: Mapped[str | None] = mapped_column(Text)
    callouts: Mapped[dict | None] = mapped_column(JSONB)

    image_key: Mapped[str] = mapped_column(Text, nullable=False)  # object storage key for the page crop
    qdrant_point_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(Text)


class Folder(Base):
    __tablename__ = "folders"

    folder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class DocumentFolder(Base):
    """Many-to-many membership -- a document can sit in zero, one, or many
    folders (like labels, not a filesystem path). Composite PK means
    re-adding a document to a folder it's already in is a harmless no-op
    rather than a duplicate row. Both FKs cascade on delete, so removing a
    document or a folder cleans up membership rows at the DB level with no
    application code needed."""

    __tablename__ = "document_folders"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.document_id", ondelete="CASCADE"), primary_key=True
    )
    folder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("folders.folder_id", ondelete="CASCADE"), primary_key=True
    )


class User(Base):
    """Login account. Remaining chat credits = credit_limit - credits_used.
    When the allowance is spent, blocked_until is set (see core/quota.py);
    once it passes, the next request refills credits_used to 0. token_version
    is bumped on password/role/active changes so existing JWTs stop working."""

    __tablename__ = "users"
    __table_args__ = (
        Index("users_username_lower_idx", text("lower(username)"), unique=True),
        CheckConstraint("role IN ('admin', 'user')", name="users_role_check"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    username: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="user")  # admin | user
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default=text("true"))
    credit_limit: Mapped[int] = mapped_column(nullable=False)
    credits_used: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_version: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageEvent(Base):
    """Audit trail of credit charges/refunds, one row per chat message."""

    __tablename__ = "usage_events"
    __table_args__ = (CheckConstraint("outcome IN ('charged', 'refunded')", name="usage_events_outcome_check"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    cost: Mapped[int] = mapped_column(nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)  # charged | refunded
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class DocumentTree(Base):
    """The tree engine's own knowledge structure for one document -- what used to be
    doc.json/tree.json/pages.json on the .rag-data volume (see rag_core/postgres_store.py).
    Keyed by the tree engine's own id ("pi-<hash>"), not documents.document_id: the two
    schemas are intentionally decoupled, same as the file-based store never knew about our
    Postgres rows either. Cleanup is explicit-order from api/documents.py::delete_document,
    not a foreign key."""

    __tablename__ = "document_trees"

    doc_id: Mapped[str] = mapped_column(Text, primary_key=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tree: Mapped[list] = mapped_column(JSONB, nullable=False)
    pages: Mapped[list] = mapped_column(JSONB, nullable=False)
    # Trigger-maintained (see alembic/versions/0008_document_trees_search.py) -- never written
    # from Python, only declared here for schema introspection.
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class DocumentPage(Base):
    """One row per page of document_trees.pages, full-text indexed so the agent's search_content
    tool can find a fact by what a page says, not only by what its document is called. Written by
    PostgresDocStore.save_document in the same transaction as the tree row; removed with its
    document by the foreign key's ON DELETE CASCADE. See alembic/versions/0011_document_pages.py."""

    __tablename__ = "document_pages"

    doc_id: Mapped[str] = mapped_column(
        Text, ForeignKey("document_trees.doc_id", ondelete="CASCADE"), primary_key=True
    )
    page_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    doc_description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # GENERATED ALWAYS AS ... STORED in the migration -- never written from Python.
    text_tsv: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)


class RateLimit(Base):
    """Fixed-window counters (see core/ratelimit.py). One row per (key, window)."""

    __tablename__ = "rate_limits"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(nullable=False, default=0)


def _asyncpg_url(dsn: str) -> str:
    if dsn.startswith("postgresql+"):
        return dsn
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


_engine = create_async_engine(
    _asyncpg_url(get_settings().postgres_dsn), pool_pre_ping=True, pool_size=10, max_overflow=20
)
async_session = async_sessionmaker(_engine, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
