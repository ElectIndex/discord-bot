"""Pure decision logic for the members-only gate. No Discord or network calls,
so every rule here is unit-tested (tests/test_membership_plan.py).

The rules, as agreed for the ElectIndex Community server:

* Members who were already in the server when the gate went live
  (joined at or before GATE_SINCE) are grandfathered: never removed.
* Anyone joining after that must arrive through electindex.com, i.e. hold a
  linked Discord account with a paid tier (or be site staff, tier "team").
  Otherwise they are removed.
* Once admitted, a member is never removed for lapsing. A lapsed membership or
  a Discord disconnect on the site only takes the tier role away.
* Tier roles belong to this bot. Each linked member holds exactly the role for
  their tier, and nobody else holds one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

TIERS = ("supporter", "patron", "founder")
ADMIT_TIERS = TIERS + ("team",)


@dataclass(frozen=True)
class Member:
    id: int
    joined_at: datetime | None
    role_ids: frozenset[int]
    bot: bool = False
    exempt: bool = False  # staff: administrators or a configured staff role


@dataclass
class Plan:
    add: dict[int, int] = field(default_factory=dict)  # member id -> role id
    remove: dict[int, set[int]] = field(default_factory=dict)  # member id -> role ids
    kick: list[int] = field(default_factory=list)
    admit: set[int] = field(default_factory=set)

    @property
    def removal_count(self) -> int:
        return len(self.remove)


def may_join(tier: str | None) -> bool:
    return tier in ADMIT_TIERS


def target_role(tier: str | None, tier_roles: dict[str, int]) -> int | None:
    return tier_roles.get(tier) if tier in TIERS else None


def plan_member(
    member: Member,
    linked: dict[int, str | None],
    tier_roles: dict[str, int],
    admitted: set[int],
    gate_since: datetime,
) -> Plan:
    """What should happen to one member, given the website's linked list."""
    plan = Plan()
    if member.bot:
        return plan

    is_linked = member.id in linked
    tier = linked.get(member.id)

    if is_linked and may_join(tier):
        plan.admit.add(member.id)

    grandfathered = member.joined_at is not None and member.joined_at <= gate_since
    if (
        not member.exempt
        and not grandfathered
        and member.id not in admitted
        and not (is_linked and may_join(tier))
    ):
        plan.kick.append(member.id)
        return plan

    want = target_role(tier, tier_roles) if is_linked else None
    held = member.role_ids & set(tier_roles.values())
    if want is not None and want not in held:
        plan.add[member.id] = want
    stale = held - ({want} if want is not None else set())
    if stale:
        plan.remove[member.id] = stale
    return plan


def plan_sync(
    members: list[Member],
    linked: dict[int, str | None],
    tier_roles: dict[str, int],
    admitted: set[int],
    gate_since: datetime,
) -> Plan:
    total = Plan()
    for member in members:
        p = plan_member(member, linked, tier_roles, admitted, gate_since)
        total.add.update(p.add)
        total.remove.update(p.remove)
        total.kick.extend(p.kick)
        total.admit |= p.admit
    return total


def removals_look_unsafe(plan: Plan, tier_role_holders: int, floor: int = 5, share: float = 0.25) -> bool:
    """A sweep that would strip roles from many members at once almost certainly
    means the website returned a bad list (outage, bug), not that a quarter of
    the paying members lapsed in the same two minutes. The caller skips the
    role REMOVALS only.

    Kicks are deliberately NOT capped. They only ever target members who joined
    after the gate and were never admitted, so a bad list can't reach anyone
    legitimate, and a cap would let a burst of uninvited joiners switch the
    gate off for everyone."""
    return plan.removal_count > max(floor, int(tier_role_holders * share))
