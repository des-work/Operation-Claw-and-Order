from datetime import datetime, timezone
from sqlalchemy import Column, Integer, BigInteger, Text, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Team(Base):
    __tablename__ = "teams"

    id = Column(Text, primary_key=True)                       # e.g. 'team-alpha'
    display_name = Column(Text, nullable=False)
    wan_ip = Column(Text, nullable=False)
    dmz_cidr = Column(Text, nullable=False)                   # e.g. '10.50.1.0/24'
    lan_cidr = Column(Text, nullable=False)                   # e.g. '10.50.2.0/24'
    bearer_token_hash = Column(Text, nullable=False)          # SHA-256 verifier or legacy bcrypt hash
    last_seen = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<Team {self.id}>"


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("uq_events_team_source", "team_id", "source_id", unique=True),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Text, ForeignKey("teams.id"), nullable=False)
    phase = Column(Integer, nullable=False)
    timestamp = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    command = Column(Text, nullable=False)
    technique = Column(Text, nullable=False)     # normalized name, 'heartbeat', or 'unknown'
    target_ip = Column(Text, nullable=False)
    result = Column(Text, nullable=False)        # success / failure / blocked / timeout / heartbeat
    milestone = Column(Text, nullable=True)      # milestone key if triggered, else NULL
    raw_output = Column(Text, nullable=True)     # truncated to 2000 chars by reporter
    source_id = Column(Text, nullable=True)      # stable reporter ID for retry deduplication


class TeamEventCount(Base):
    __tablename__ = "team_event_counts"

    team_id = Column(Text, ForeignKey("teams.id"), primary_key=True)
    event_count = Column(BigInteger, nullable=False, default=0)


class DashboardOutbox(Base):
    __tablename__ = "dashboard_outbox"
    __table_args__ = (
        Index("ix_dashboard_outbox_pending", "delivered_at", "event_id"),
        Index("ix_dashboard_outbox_age", "delivered_at", "created_at"),
    )

    event_id = Column(Integer, ForeignKey("events.id"), primary_key=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    delivered_at = Column(DateTime(timezone=True), nullable=True)


class Milestone(Base):
    __tablename__ = "milestones"
    __table_args__ = (
        UniqueConstraint("team_id", "key", name="uq_milestone_team_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Text, ForeignKey("teams.id"), nullable=False)
    key = Column(Text, nullable=False)           # dmz_compromise / lan_pivot / etc.
    triggering_event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    score = Column(Integer, nullable=False, default=0)
    recorded_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<Milestone team={self.team_id} key={self.key} score={self.score}>"


class PhaseLog(Base):
    __tablename__ = "phase_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phase = Column(Integer, nullable=False)
    activated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    activated_by = Column(Text, nullable=False)  # 'admin' or faculty username


class DiscordOutbox(Base):
    __tablename__ = "discord_outbox"
    __table_args__ = (
        Index("ix_discord_outbox_due", "sent_at", "next_attempt"),
        Index("ix_discord_outbox_age", "sent_at", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    dedupe_key = Column(Text, unique=True, nullable=False)
    message_type = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    next_attempt = Column(DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(timezone.utc))
    attempts = Column(Integer, nullable=False, default=0)
    sent_at = Column(DateTime(timezone=True), nullable=True)
