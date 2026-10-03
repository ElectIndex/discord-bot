"""Server-specific ids. Defaults are the ElectIndex Community server; each can be
overridden from the environment so the code stays reusable."""

import os


def _id(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


WELCOME_CHANNEL_ID = _id("WELCOME_CHANNEL_ID", 1529620546634125464)  # #welcome-users
LOG_CHANNEL_ID = _id("LOG_CHANNEL_ID", 1529620332694995044)  # #server-logs
MEMBER_ROLE_ID = _id("MEMBER_ROLE_ID", 1529620718159925418)  # given to everyone admitted
STAFF_ROLE_IDS = {int(r) for r in os.environ.get("STAFF_ROLE_IDS", "1529621115977072662").split(",") if r.strip()}

INFO_CHANNEL_ID = _id("INFO_CHANNEL_ID", 1529611570186027160)  # #info-and-about
RULES_CHANNEL_ID = _id("RULES_CHANNEL_ID", 1529611483095236689)  # #community-rules

# The role menu in #info-and-about (cogs/info.py): (role id, label, emoji, description).
SELF_ROLES = (
    (1532854602490183720, "ElectIndex Updates", "🗳️", "pings for new forecasts, articles and site updates"),
    (1532854639433617610, "Community Updates", "🫂", "pings for server news, events and polls"),
)

# Legacy reaction roles: message id -> {emoji: role id}. The old YAGPDB menu was
# replaced by the button menu; add an entry here only to revive a reaction menu.
REACTION_ROLES: dict[int, dict[str, int]] = {}
