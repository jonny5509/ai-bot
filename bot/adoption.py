from __future__ import annotations

import logging
import re
import uuid

import discord


class AdoptionDecisionButton(discord.ui.DynamicItem[discord.ui.Button],
                             template=r"adopt:(?P<action>accept|decline):(?P<request_id>[a-f0-9]{16})"):
    def __init__(self, action: str, request_id: str):
        self.action = action
        self.request_id = request_id
        label = "Accept adoption request" if action == "accept" else "Decline"
        style = discord.ButtonStyle.success if action == "accept" else discord.ButtonStyle.secondary
        super().__init__(discord.ui.Button(
            label=label, style=style, custom_id=f"adopt:{action}:{request_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Button, match: re.Match[str]):
        return cls(match.group("action"), match.group("request_id"))

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        request = bot.storage.get_adoption_request(self.request_id)
        if not request or request["status"] != "pending":
            await interaction.response.send_message(
                "This adoption request is no longer pending.", ephemeral=True)
            return
        if interaction.user.id != int(request["target_id"]):
            await interaction.response.send_message(
                "Only the person who received this adoption request can respond.", ephemeral=True)
            return

        status = "accepted" if self.action == "accept" else "declined"
        if not bot.storage.set_adoption_request_status(self.request_id, status):
            await interaction.response.send_message(
                "This request has already been answered.", ephemeral=True)
            return

        if status == "accepted":
            text = (
                "Adoption request accepted. You can now coordinate with each other. "
                "No EmlaLock session or account relationship has been changed by Discord."
            )
        else:
            text = "Adoption request declined. No EmlaLock changes were made."
        await interaction.response.edit_message(content=text, view=None)
        requester = bot.get_user(int(request["requester_id"]))
        if requester is None:
            try:
                requester = await bot.fetch_user(int(request["requester_id"]))
            except (discord.NotFound, discord.HTTPException):
                requester = None
        if requester:
            try:
                if status == "accepted":
                    await requester.send(
                        f"<@{request['target_id']}> accepted your adoption request. "
                        "You can now coordinate on EmlaLock. The bot has not changed either EmlaLock account or session; "
                        "the wearer/keyholder relationship must be set up on EmlaLock itself."
                    )
                else:
                    await requester.send(f"<@{request['target_id']}> declined your adoption request.")
            except discord.HTTPException:
                logging.info("Could not DM the requester about adoption request %s.", self.request_id)


class AdoptionRequestView(discord.ui.View):
    def __init__(self, bot, request_id: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.add_item(AdoptionDecisionButton("accept", request_id))
        self.add_item(AdoptionDecisionButton("decline", request_id))


class AdoptionCandidateSelect(discord.ui.Select):
    def __init__(self, candidates, direction: str):
        self.direction = direction
        options = [
            discord.SelectOption(
                label=discord.utils.escape_markdown(member.display_name)[:100],
                description=f"Discord member {member.name}"[:100],
                value=str(member.id),
            )
            for member in candidates[:25]
        ]
        super().__init__(
            placeholder="Choose someone to send an adoption request to",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.send_request(interaction, int(self.values[0]))


class AdoptionCandidateView(discord.ui.View):
    def __init__(self, bot, requester_id: int, candidates, direction: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.requester_id = requester_id
        self.candidates = {member.id: member for member in candidates[:25]}
        self.direction = direction
        self.add_item(AdoptionCandidateSelect(candidates, direction))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who opened this list can choose a member.", ephemeral=True)
            return False
        return True

    async def send_request(self, interaction: discord.Interaction, target_id: int):
        target = self.candidates.get(target_id)
        if target is None or interaction.guild is None:
            await interaction.response.send_message(
                "That member is no longer available. Please open the adoption panel again.", ephemeral=True)
            return
        requester_account = self.bot.storage.get_account(self.requester_id)
        target_account = self.bot.storage.get_account(target_id)
        if not requester_account or not target_account:
            await interaction.response.send_message(
                "Both people must link their EmlaLock accounts with /link before sending an adoption request.",
                ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        request_id = uuid.uuid4().hex[:16]
        self.bot.storage.create_adoption_request(
            request_id, interaction.guild.id, self.requester_id, target_id)
        try:
            await target.send(
                f"**Adoption request from {discord.utils.escape_markdown(interaction.user.display_name)}**\n"
                "They would like to connect with you as their EmlaLock wearer/keyholder counterpart. "
                "Accepting only confirms your interest in Discord; it does not alter EmlaLock.",
                view=AdoptionRequestView(self.bot, request_id),
            )
        except discord.Forbidden:
            self.bot.storage.set_adoption_request_status(request_id, "undeliverable")
            await interaction.followup.send(
                "I couldn't DM that member. They may need to enable server-member DMs. No request was sent.",
                ephemeral=True)
            return
        except discord.HTTPException:
            self.bot.storage.set_adoption_request_status(request_id, "undeliverable")
            await interaction.followup.send(
                "Discord couldn't deliver the request. Please try again later.", ephemeral=True)
            return

        await interaction.followup.send(
            f"Adoption request sent privately to {target.mention}. I'll let you know when they respond.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.stop()


class AdoptionPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _browse(self, interaction: discord.Interaction, direction: str):
        if not interaction.guild:
            await interaction.response.send_message("Use this panel in a server.", ephemeral=True)
            return
        settings = self.bot.storage.get_adoption_roles(interaction.guild.id)
        if not settings:
            await interaction.response.send_message(
                "An administrator needs to run /setupadoption first.", ephemeral=True)
            return
        requester_role_id = int(settings[
            "looking_for_keyholder_role_id" if direction == "keyholder" else "looking_for_wearer_role_id"
        ])
        candidate_role_id = int(settings[
            "looking_for_wearer_role_id" if direction == "keyholder" else "looking_for_keyholder_role_id"
        ])
        if not any(role.id == requester_role_id for role in interaction.user.roles):
            await interaction.response.send_message(
                "Your linked EmlaLock session does not currently qualify for this adoption role. "
                "The bot refreshes these roles every five minutes.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        members = [member async for member in interaction.guild.fetch_members(limit=None)]
        candidates = [
            member for member in members
            if member.id != interaction.user.id
            and not member.bot
            and any(role.id == candidate_role_id for role in member.roles)
            and self.bot.storage.get_account(member.id)
        ]
        if not candidates:
            await interaction.followup.send(
                "There are no linked members with the opposite adoption role right now.", ephemeral=True)
            return
        label = "keyholder" if direction == "keyholder" else "wearer"
        await interaction.followup.send(
            f"Choose a {label} to send a private adoption request to:",
            view=AdoptionCandidateView(self.bot, interaction.user.id, candidates, direction),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Find a Keyholder", style=discord.ButtonStyle.primary,
        custom_id="adoption_panel:find_keyholder")
    async def find_keyholder(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._browse(interaction, "keyholder")

    @discord.ui.button(
        label="Find a Wearer", style=discord.ButtonStyle.success,
        custom_id="adoption_panel:find_wearer")
    async def find_wearer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._browse(interaction, "wearer")
