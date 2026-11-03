from datetime import datetime, timezone
from sqlalchemy import Column, Integer, BigInteger, Boolean, Text, DateTime, ForeignKey, UniqueConstraint, Index
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


class DiscordWebhookDelivery(Base):
    __tablename__ = "discord_webhook_deliveries"
    __table_args__ = (Index("ix_discord_webhook_due", "sent_at", "next_attempt"),)

    outbox_id = Column(Integer, ForeignKey("discord_outbox.id"), primary_key=True)
    destination = Column(Text, primary_key=True)
    next_attempt = Column(DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(timezone.utc))
    attempts = Column(Integer, nullable=False, default=0)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    message_id = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)


class DiscordWebhookDestination(Base):
    __tablename__ = "discord_webhook_destinations"

    name = Column(Text, primary_key=True)
    credential_hash = Column(Text, nullable=False)
    blocked = Column(Boolean, nullable=False, default=False)
    retry_after = Column(DateTime(timezone=True), nullable=False,
                         default=lambda: datetime.now(timezone.utc))


class Student(Base):
    __tablename__ = "students"
    id = Column(Text, primary_key=True)
    email = Column(Text, nullable=False, unique=True)
    display_name = Column(Text, nullable=False)
    discord_user_id = Column(Text, nullable=True, unique=True)


class Enrollment(Base):
    __tablename__ = "enrollments"
    student_id = Column(Text, ForeignKey("students.id"), primary_key=True)
    term_id = Column(Text, primary_key=True)
    class_id = Column(Text, primary_key=True)
    team_id = Column(Text, ForeignKey("teams.id"), nullable=True)
    role_key = Column(Text, nullable=True)
    idea = Column(Text, nullable=False, default="")
    status = Column(Text, nullable=False, default="active")


class EnrollmentState(Base):
    __tablename__ = "enrollment_state"
    id = Column(Integer, primary_key=True, default=1)
    revision = Column(Integer, nullable=False, default=0)


class EnrollmentImport(Base):
    __tablename__ = "enrollment_imports"
    id = Column(Text, primary_key=True)
    payload_json = Column(Text, nullable=False)
    digest = Column(Text, nullable=False)
    policy_hash = Column(Text, nullable=False)
    base_revision = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    applied_at = Column(DateTime(timezone=True), nullable=True)


class RegistrationInvitation(Base):
    __tablename__ = "registration_invitations"
    code_hash = Column(Text, primary_key=True)
    student_id = Column(Text, ForeignKey("students.id"), nullable=False, index=True)
    guild_id = Column(Text, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    revoked = Column(Boolean, nullable=False, default=False)


class MembershipSync(Base):
    __tablename__ = "membership_sync"
    __table_args__ = (Index("ix_membership_sync_due", "guild_id", "status", "next_attempt"),)
    student_id = Column(Text, ForeignKey("students.id"), primary_key=True)
    guild_id = Column(Text, primary_key=True)
    policy_hash = Column(Text, nullable=False)
    revision = Column(Integer, nullable=False)
    status = Column(Text, nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt = Column(DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(timezone.utc))
    last_error = Column(Text, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    queued_at = Column(DateTime(timezone=True), nullable=False,
                       default=lambda: datetime.now(timezone.utc))


class ManagedDiscordGrant(Base):
    __tablename__ = "managed_discord_grants"
    student_id = Column(Text, ForeignKey("students.id"), primary_key=True)
    guild_id = Column(Text, primary_key=True)
    role_id = Column(Text, primary_key=True)


class EnrollmentAudit(Base):
    __tablename__ = "enrollment_audit"
    id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(Text, nullable=False)
    subject_id = Column(Text, nullable=False)
    revision = Column(Integer, nullable=False)
    details_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))


class EnrollmentWorker(Base):
    __tablename__ = "enrollment_workers"
    guild_id = Column(Text, primary_key=True)
    policy_hash = Column(Text, nullable=False)
    last_seen = Column(DateTime(timezone=True), nullable=False)
    last_error = Column(Text, nullable=True)
