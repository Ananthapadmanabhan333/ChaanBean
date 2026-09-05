"""Import batches, per-row provenance, ERP connections and sync runs.

Every fetched record keeps its raw payload alongside the parsed result. Sources
change shape without notice and entity matching improves — you will need to
re-parse years of history without re-fetching it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, _uuid_pk


class ImportBatch(Base, TimestampMixin):
    """One upload. Nothing reaches the ledger until a human confirms the preview."""

    __tablename__ = "import_batches"
    __table_args__ = (Index("ix_import_batches_company", "company_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="buyers", nullable=False)
    # PENDING -> PREVIEWED -> COMMITTED | ABANDONED | FAILED
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(512))
    date_format: Mapped[str] = mapped_column(String(20), default="%d/%m/%Y", nullable=False)

    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    create_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    update_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reject_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    committed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    rows: Mapped[list[ImportRow]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class ImportRow(Base):
    """One line of the file: what arrived, what we made of it, and what we decided."""

    __tablename__ = "import_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_number"),
        Index("ix_import_rows_batch_action", "batch_id", "action"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batches.id"), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)

    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    parsed: Mapped[dict | None] = mapped_column(JSONB)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # CREATE/UPDATE/...
    reason: Mapped[str | None] = mapped_column(Text)
    matched_buyer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("buyers.id"))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    batch: Mapped[ImportBatch] = relationship(back_populates="rows")


class ErpConnection(Base, TimestampMixin):
    """Credentials are encrypted at rest; this row never holds a usable secret in
    clear, so a database dump is not an ERP compromise."""

    __tablename__ = "erp_connections"
    __table_args__ = (UniqueConstraint("company_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # tally | zoho_books
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    date_format: Mapped[str] = mapped_column(String(20), default="%d/%m/%Y", nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(32))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_connection", "connection_id", "started_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("erp_connections.id"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="RUNNING", nullable=False)

    fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unchanged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    flagged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)


class ProviderFetch(Base):
    """Raw payload from any external source, kept forever (rule 10).

    Sources change shape without notice and entity matching improves; you will
    need to re-parse years of history without re-fetching it.
    """

    __tablename__ = "provider_fetches"
    __table_args__ = (
        Index("ix_provider_fetches_lookup", "company_id", "provider", "external_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    resource: Mapped[str] = mapped_column(String(32), nullable=False)  # invoice | payment
    external_id: Mapped[str | None] = mapped_column(String(128))
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    parsed: Mapped[dict | None] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
