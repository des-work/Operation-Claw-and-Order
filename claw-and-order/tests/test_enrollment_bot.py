"""Exercise permission preflight and private slash registration without a gateway."""

from types import SimpleNamespace

import discord
import pytest
from sqlalchemy import select

from backend import enrollment_bot
from backend.enrollment_bot import DiscordMembershipAdapter, EnrollmentBot
from backend.models import Student
from backend.services.enrollment import issue_invitations
from backend.services.membership_sync import MembershipError
from tests.conftest import _session_factory
from tests.test_enrollment import (enrollment_setup, apply_rows, row, GUILD, USER, CLASS_A, TEAM_A)


class Role:
    def __init__(self, role_id, position=1, permissions=0):
        self.id, self.position, self.managed = int(role_id), position, False
        self.permissions = SimpleNamespace(value=permissions)
    def is_default(self):
        return str(self.id) == GUILD
    def __ge__(self, other):
        return self.position >= other.position


class FakeClient:
    def __init__(self, policy):
        self.user = SimpleNamespace(id=970000000000000001)
        me = SimpleNamespace(id=self.user.id, top_role=Role("980000000000000001", 100),
                             guild_permissions=SimpleNamespace(manage_roles=True, administrator=False))
        roles = [Role(GUILD, 0)] + [Role(role_id) for role_id in policy.managed_roles]
        self.guild = SimpleNamespace(roles=roles, default_role=roles[0])
        async def fetch_member(_):
            return me
        self.guild.fetch_member = fetch_member
        self.allowed = {}
        channels = []
        for c in policy.data["classes"]:
            for team in c["teams"].values():
                for channel_id in team["channel_ids"]:
                    allowed = {self.user.id, int(team["discord_role_id"])}
                    self.allowed[channel_id] = allowed
                    channels.append(SimpleNamespace(id=int(channel_id), permissions_for=lambda member, allowed=allowed:
                        SimpleNamespace(view_channel=member.id in allowed)))
        async def fetch_channels():
            return channels
        self.guild.fetch_channels = fetch_channels
    async def fetch_guild(self, _):
        return self.guild
    async def fetch_invite(self, _):
        return SimpleNamespace(guild=SimpleNamespace(id=int(GUILD)))


@pytest.mark.asyncio
async def test_preflight_blocks_cross_team_access_and_privileged_roles(enrollment_setup):
    policy = enrollment_setup
    client = FakeClient(policy)
    adapter = DiscordMembershipAdapter(client, policy)
    await adapter.validate(policy, policy.managed_roles, check_invite=True)
    client.allowed["950000000000000001"].add(int(CLASS_A))
    with pytest.raises(MembershipError, match="another managed"):
        await adapter.validate(policy, policy.managed_roles)
    client.allowed["950000000000000001"].remove(int(CLASS_A))
    next(role for role in client.guild.roles if str(role.id) == TEAM_A).permissions.value = discord.Permissions.administrator.flag
    with pytest.raises(MembershipError, match="zero server permissions"):
        await adapter.validate(policy, policy.managed_roles)


@pytest.mark.asyncio
async def test_register_command_uses_interaction_identity_and_private_replies(enrollment_setup, monkeypatch):
    policy = enrollment_setup
    await apply_rows([row()], policy)
    async with _session_factory() as session, session.begin():
        code = (await issue_invitations(session, ["s1"], 72, policy))[0]["registration_code"]
    monkeypatch.setattr(enrollment_bot, "async_session", _session_factory)
    bot = EnrollmentBot(policy)
    assert not bot.intents.message_content and not bot.intents.members
    command = bot.tree.get_commands(guild=discord.Object(id=int(GUILD)))[0]
    replies = []
    async def defer(**kwargs):
        assert kwargs["ephemeral"]
    async def send(message, **kwargs):
        assert kwargs["ephemeral"]
        replies.append(message)
    interaction = SimpleNamespace(user=SimpleNamespace(id=int(USER)), guild_id=int(GUILD),
        response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=send))
    await command.callback(interaction, code)
    async with _session_factory() as session:
        assert (await session.get(Student, "s1")).discord_user_id == USER
    assert replies and all(code not in message for message in replies)
    await bot.close()


@pytest.mark.asyncio
async def test_effective_member_permissions_detect_unmanaged_cross_team_leak(enrollment_setup):
    policy = enrollment_setup
    client = FakeClient(policy)
    async def student(_):
        return SimpleNamespace(id=int(USER))
    adapter = DiscordMembershipAdapter(client, policy)
    await adapter.validate(policy, policy.managed_roles)
    client.guild.fetch_member = student
    client.allowed["950000000000000001"].add(int(USER))
    await adapter.verify_access(USER, {CLASS_A, TEAM_A})
    client.allowed["950000000000000002"].add(int(USER))
    with pytest.raises(MembershipError, match="Effective member") as failure:
        await adapter.verify_access(USER, {CLASS_A, TEAM_A})
    assert failure.value.status == "blocked_isolation"
