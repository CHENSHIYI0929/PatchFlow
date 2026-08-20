from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RunRecord(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    agent_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    description: Mapped[str] = mapped_column(Text)
    repo_path: Mapped[str] = mapped_column(Text)
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    artifact_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class RunEventRecord(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)


class RunStore:
    def __init__(self, database_url: str) -> None:
        options = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self.engine = create_engine(database_url, pool_pre_ping=True, connect_args=options)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def init_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def create_run(self, run_id: str, description: str, repo_path: str, request: dict[str, Any]) -> dict[str, Any]:
        with self.sessions.begin() as session:
            record = RunRecord(
                id=run_id,
                status="queued",
                description=description,
                repo_path=repo_path,
                request=request,
            )
            session.add(record)
        return self._run_dict(record)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.sessions() as session:
            record = session.get(RunRecord, run_id)
            return self._run_dict(record) if record else None

    def update_run(self, run_id: str, **values: Any) -> dict[str, Any]:
        with self.sessions.begin() as session:
            record = session.get(RunRecord, run_id)
            if record is None:
                raise KeyError(run_id)
            for key, value in values.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            record.updated_at = _utcnow()
        return self._run_dict(record)

    def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.sessions.begin() as session:
            last = session.scalar(
                select(RunEventRecord.sequence)
                .where(RunEventRecord.run_id == run_id)
                .order_by(RunEventRecord.sequence.desc())
                .limit(1)
            )
            record = RunEventRecord(
                run_id=run_id,
                sequence=(last or 0) + 1,
                event_type=event_type,
                payload=payload,
            )
            session.add(record)
        return self._event_dict(record)

    def list_events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(RunEventRecord)
                .where(RunEventRecord.run_id == run_id, RunEventRecord.sequence > after)
                .order_by(RunEventRecord.sequence)
            ).all()
            return [self._event_dict(row) for row in rows]

    def replace_artifacts(self, run_id: str, artifact_dir: str) -> None:
        from pathlib import Path

        root = Path(artifact_dir)
        with self.sessions.begin() as session:
            for path in sorted(root.iterdir()) if root.exists() else []:
                if path.is_file():
                    session.add(ArtifactRecord(
                        run_id=run_id,
                        name=path.name,
                        path=str(path.resolve()),
                        size_bytes=path.stat().st_size,
                    ))

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(ArtifactRecord).where(ArtifactRecord.run_id == run_id).order_by(ArtifactRecord.name)
            ).all()
            return [{"name": row.name, "path": row.path, "size_bytes": row.size_bytes} for row in rows]

    @staticmethod
    def _run_dict(record: RunRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "status": record.status,
            "agent_status": record.agent_status,
            "description": record.description,
            "repo_path": record.repo_path,
            "request": record.request,
            "result": record.result,
            "artifact_dir": record.artifact_dir,
            "failure_type": record.failure_type,
            "failure_stage": record.failure_stage,
            "failure_message": record.failure_message,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }

    @staticmethod
    def _event_dict(record: RunEventRecord) -> dict[str, Any]:
        return {
            "sequence": record.sequence,
            "event_type": record.event_type,
            "payload": record.payload,
            "created_at": record.created_at.isoformat(),
        }
