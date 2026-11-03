"""Separate Cyberlabs enrollment worker: python -m backend.enrollment_bot.

Uses an official bot identity, private slash-command registration, and the shared
enrollment database. It never reads message content or sends invitation emails.
"""

import asyncio
import argparse
import logging
import os

import discord
from discord import app_commands

from backend.database import async_session
from backend.enrollment_policy import EnrollmentError, load_policy
from backend.services.enrollment import claim_registration
from backend.services.membership_sync import MembershipError, sync_once, heartbeat

log = logging.getLogger("enrollment_bot")


class DiscordMembershipAdapter:
    def __init__(self, client, policy):
        self.client, self.policy = client, policy
        self.guild = None

    async def validate(self, policy, controlled, check_invite=False):
        try:
            guild = await self.client.fetch_guild(int(policy.guild_id))
            me = await guild.fetch_member(self.client.user.id)
            by_id = {str(role.id): role for role in guild.roles}
            if not me.guild_permissions.manage_roles:
                raise MembershipError("Enrollment bot lacks Manage Roles", "blocked_permissions")
            if me.guild_permissions.administrator:
                raise MembershipError("Enrollment bot must not have Administrator", "blocked_permissions")
            for role_id in controlled:
                role = by_id.get(role_id)
                if role is None:
                    # A retired role that was deleted has no access left to remove.
                    if role_id not in policy.managed_roles:
                        continue
                    raise MembershipError("A configured Discord role is missing", "blocked_permissions")
                if role.managed or role.is_default() or role.permissions.value != 0 or role >= me.top_role:
                    raise MembershipError("Managed student roles must have zero server permissions and sit below the bot", "blocked_permissions")
            channels = {str(channel.id): channel for channel in await guild.fetch_channels()}
            for c in policy.data["classes"]:
                for team in c["teams"].values():
                    own_role = by_id[team["discord_role_id"]]
                    for channel_id in team["channel_ids"]:
                        channel = channels.get(channel_id)
                        if channel is None or not hasattr(channel, "permissions_for"):
                            raise MembershipError("A configured private team channel is missing", "blocked_permissions")
                        if not channel.permissions_for(me).view_channel or channel.permissions_for(guild.default_role).view_channel:
                            raise MembershipError("Team channel must be private and visible to the enrollment bot", "blocked_permissions")
                        if not channel.permissions_for(own_role).view_channel:
                            raise MembershipError("Team role cannot view its configured team channel", "blocked_permissions")
                        for role_id in policy.managed_roles - {team["discord_role_id"]}:
                            if channel.permissions_for(by_id[role_id]).view_channel:
                                raise MembershipError("Team channel grants access to another managed class, team or responsibility role", "blocked_permissions")
            if check_invite:
                invite = await self.client.fetch_invite(policy.data["invite_url"])
                if not invite.guild or str(invite.guild.id) != policy.guild_id:
                    raise MembershipError("Invite does not belong to the selected server", "blocked_permissions")
            self.guild = guild
        except MembershipError:
            raise
        except (discord.Forbidden, discord.NotFound):
            raise MembershipError("Enrollment bot cannot access the selected server, invite or role mapping", "blocked_permissions") from None
        except discord.HTTPException:
            raise MembershipError("Discord is temporarily unavailable") from None

    async def member_roles(self, user_id):
        try:
            member = await self.guild.fetch_member(int(user_id))
            return {str(role.id) for role in member.roles}
        except discord.NotFound:
            raise MembershipError("Student is not currently in the selected server", "awaiting_member") from None
        except discord.Forbidden:
            raise MembershipError("Enrollment bot cannot inspect the member", "blocked_permissions") from None
        except discord.HTTPException:
            raise MembershipError("Discord member lookup temporarily failed") from None

    async def _change(self, user_id, role_id, reason, add):
        member = discord.Object(id=int(user_id))
        # Use public Member methods, which patch one role and preserve others.
        try:
            member = await self.guild.fetch_member(member.id)
            role = discord.Object(id=int(role_id))
            if add:
                await member.add_roles(role, reason=reason, atomic=True)
            else:
                await member.remove_roles(role, reason=reason, atomic=True)
        except discord.NotFound:
            raise MembershipError("Student or role disappeared during reconciliation", "awaiting_member") from None
        except discord.Forbidden:
            raise MembershipError("Discord rejected a role change; repair permissions and review reconciliation", "blocked_permissions") from None
        except discord.HTTPException:
            raise MembershipError("Discord role change temporarily failed") from None

    async def add_role(self, user_id, role_id, reason):
        await self._change(user_id, role_id, reason, True)

    async def remove_role(self, user_id, role_id, reason):
        await self._change(user_id, role_id, reason, False)


class EnrollmentBot(discord.Client):
    def __init__(self, policy):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.policy = policy
        self.tree = app_commands.CommandTree(self)
        self.adapter = DiscordMembershipAdapter(self, policy)
        self.worker = None

        @app_commands.command(name="register", description="Link your private college enrollment code to this Discord account")
        @app_commands.guild_only()
        @app_commands.checks.cooldown(3, 60, key=lambda interaction: (interaction.guild_id, interaction.user.id))
        async def register(interaction: discord.Interaction, code: str):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                current = load_policy()
                if current.digest != self.policy.digest:
                    raise EnrollmentError("Enrollment configuration changed; ask staff to restart the enrollment worker", 503)
                async with async_session() as session, session.begin():
                    await claim_registration(session, code, str(interaction.user.id), str(interaction.guild_id), current)
                await interaction.followup.send("Your account is registered. Approved roles are queued. Students without a team receive their configured class access once processing completes; staff can review any pending assignments.", ephemeral=True)
            except EnrollmentError as error:
                await interaction.followup.send(str(error), ephemeral=True)
            except Exception:
                log.error("Registration database operation failed")
                await interaction.followup.send("Registration is temporarily unavailable. Please retry or contact an instructor.", ephemeral=True)

        @register.error
        async def registration_error(interaction, error):
            message = "Please wait before trying registration again." if isinstance(error, app_commands.CommandOnCooldown) else "Registration could not be completed. Contact an instructor."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

        self.tree.add_command(register, guild=discord.Object(id=int(policy.guild_id)))

    async def setup_hook(self):
        await self.adapter.validate(self.policy, self.policy.managed_roles, check_invite=True)
        await self.tree.sync(guild=discord.Object(id=int(self.policy.guild_id)))
        self.worker = asyncio.create_task(self._work())

    async def _work(self):
        while True:
            try:
                await heartbeat(async_session, self.policy)
                attempted = await sync_once(async_session, self.policy, self.adapter)
                if not attempted:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error("Enrollment worker operation failed; queued work is retained")
                try:
                    await heartbeat(async_session, self.policy, "worker_operation_failed")
                except Exception:
                    pass
                await asyncio.sleep(5)

    async def close(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        await super().close()


async def main(check_only=False):
    policy = load_policy()
    token = os.getenv("ENROLLMENT_DISCORD_BOT_TOKEN", "")
    if not token:
        raise EnrollmentError("Set ENROLLMENT_DISCORD_BOT_TOKEN in the worker secret store", 503)
    if check_only:
        async with discord.Client(intents=discord.Intents.none()) as client:
            await client.login(token)
            await DiscordMembershipAdapter(client, policy).validate(policy, policy.managed_roles, check_invite=True)
        print("Discord enrollment preflight passed; no commands, memberships or roles were changed")
        return
    from backend.config import DATABASE_URL
    from sqlalchemy import text
    if os.getenv("APP_ENV") == "production" and not DATABASE_URL.startswith("postgresql+asyncpg://"):
        raise EnrollmentError("Production enrollment requires a PostgreSQL asyncpg DATABASE_URL", 503)
    async with async_session() as session:
        for table in ("enrollment_state", "students", "enrollments", "enrollment_imports", "registration_invitations",
                      "membership_sync", "managed_discord_grants", "enrollment_audit", "enrollment_workers"):
            await session.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
    async with EnrollmentBot(policy) as bot:
        await bot.start(token)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify the configured server without changing commands or roles")
    try:
        asyncio.run(main(parser.parse_args().check))
    except (EnrollmentError, MembershipError) as error:
        raise SystemExit(str(error)) from None
    except discord.LoginFailure:
        raise SystemExit("Enrollment bot login failed; check its private token") from None
