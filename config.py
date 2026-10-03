"""Server-specific ids. Defaults are the ElectIndex Community server; each can be
overridden from the environment so the code stays reusable."""

import os


def _id(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


WELCOME_CHANNEL_ID = _id("WELCOME_CHANNEL_ID", 1529620546634125464)  # #welcome-users
LOG_CHANNEL_ID = _id("LOG_CHANNEL_ID", 1529620332694995044)  # #server-logs
MEMBER_ROLE_ID = _id("MEMBER_ROLE_ID", 1529620718159925418)  # given to everyone admitted
STAFF_ROLE_IDS = {int(r) for r in os.environ.get("STAFF_ROLE_IDS", "1529621115977072662").split(",") if r.strip()}

# Reaction roles: message id -> {emoji: role id}. The message is the menu YAGPDB
# posted in #info-and-about, kept so nobody has to re-react.
REACTION_ROLES = {
    1532854944972017754: {
        "🗳️": 1532854602490183720,  # ElectIndex Updates
        "🫂": 1532854639433617610,  # Community Updates
    },
}
