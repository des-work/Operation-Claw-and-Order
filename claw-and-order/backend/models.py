from datetime import datetime, timezone
from sqlalchemy import Column, Integer, Text, DateTime, ForeignKey, UniqueConstraint
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
    bearer_token_hash = Column(Text, nullable=False)          # bcrypt hash
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
