import unittest
from datetime import datetime, timedelta, timezone

from membership_plan import Invite, Member, claim_invite, plan_member, plan_sync, removals_look_unsafe

GATE = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)
BEFORE = GATE - timedelta(days=30)
AFTER = GATE + timedelta(hours=1)
ROLES = {"supporter": 101, "patron": 102, "founder": 103}


def m(id, joined=AFTER, roles=(), bot=False, exempt=False):
    return Member(id=id, joined_at=joined, role_ids=frozenset(roles), bot=bot, exempt=exempt)


def plan(member, linked=None, admitted=()):
    return plan_member(member, linked or {}, ROLES, set(admitted), GATE)


class GateTests(unittest.TestCase):
    def test_grandfathered_unlinked_member_is_left_alone(self):
        p = plan(m(1, joined=BEFORE, roles={7}))
        self.assertEqual((p.kick, p.add, p.remove), ([], {}, {}))

    def test_new_unlinked_joiner_is_removed(self):
        self.assertEqual(plan(m(1)).kick, [1])

    def test_new_joiner_linked_without_a_tier_is_removed(self):
        self.assertEqual(plan(m(1), {1: None}).kick, [1])

    def test_new_paid_joiner_is_admitted_with_their_role(self):
        p = plan(m(1), {1: "patron"})
        self.assertEqual((p.kick, p.add, p.admit), ([], {1: 102}, {1}))

    def test_team_joins_without_a_tier_role(self):
        p = plan(m(1), {1: "team"})
        self.assertEqual((p.kick, p.add, p.admit), ([], {}, {1}))

    def test_staff_and_bots_are_never_removed(self):
        self.assertEqual(plan(m(1, exempt=True)).kick, [])
        self.assertEqual(plan(m(2, bot=True)).kick, [])

    def test_unknown_join_date_is_not_grandfathered(self):
        self.assertEqual(plan(m(1, joined=None)).kick, [1])


class LapseTests(unittest.TestCase):
    def test_admitted_member_who_lapses_keeps_their_seat_but_loses_the_role(self):
        p = plan(m(1, roles={102}), {1: None}, admitted={1})
        self.assertEqual((p.kick, p.remove), ([], {1: {102}}))

    def test_admitted_member_who_disconnects_loses_the_role(self):
        p = plan(m(1, roles={102}), {}, admitted={1})
        self.assertEqual((p.kick, p.remove), ([], {1: {102}}))

    def test_upgrade_swaps_the_role(self):
        p = plan(m(1, roles={101}), {1: "founder"}, admitted={1})
        self.assertEqual((p.add, p.remove), ({1: 103}, {1: {101}}))

    def test_correct_role_means_no_change(self):
        p = plan(m(1, roles={102, 7}), {1: "patron"}, admitted={1})
        self.assertEqual((p.add, p.remove, p.kick), ({}, {}, []))

    def test_tier_role_handed_out_by_hand_is_taken_back(self):
        p = plan(m(1, joined=BEFORE, roles={103}))
        self.assertEqual(p.remove, {1: {103}})

    def test_grandfathered_supporter_who_links_gets_the_role(self):
        p = plan(m(1, joined=BEFORE), {1: "supporter"})
        self.assertEqual((p.add, p.kick), ({1: 101}, []))


class SafetyTests(unittest.TestCase):
    def test_empty_list_from_the_website_is_refused(self):
        members = [m(i, joined=BEFORE, roles={101}) for i in range(40)]
        p = plan_sync(members, {}, ROLES, set(), GATE)
        self.assertEqual(p.removal_count, 40)
        self.assertTrue(removals_look_unsafe(p, tier_role_holders=40))

    def test_a_few_lapses_are_fine(self):
        members = [m(i, joined=BEFORE, roles={101}) for i in range(40)]
        linked = {i: "supporter" for i in range(37)}
        p = plan_sync(members, linked, ROLES, set(), GATE)
        self.assertEqual(p.removal_count, 3)
        self.assertFalse(removals_look_unsafe(p, tier_role_holders=40))

    def test_a_burst_of_uninvited_joiners_is_still_removed(self):
        # Ten alts through a leaked invite must not be able to switch the gate off.
        p = plan_sync([m(i) for i in range(10)], {}, ROLES, set(), GATE)
        self.assertEqual(len(p.kick), 10)
        self.assertFalse(removals_look_unsafe(p, tier_role_holders=0))

    def test_a_bad_list_can_never_kick_admitted_or_grandfathered_members(self):
        members = [m(1, joined=BEFORE, roles={101}), m(2, roles={102})]
        p = plan_sync(members, {}, ROLES, {2}, GATE)
        self.assertEqual(p.kick, [])


class InviteTests(unittest.TestCase):
    def test_the_invite_whose_count_went_up_is_claimed(self):
        seen = {"a": Invite(3, 0, 1), "b": Invite(0, 0, 2)}
        code, after = claim_invite(seen, {"a": Invite(3, 0, 1), "b": Invite(1, 0, 2)})
        self.assertEqual(code, "b")
        self.assertEqual(after["b"].uses, 1)

    def test_a_join_without_an_invite_claims_nothing(self):
        seen = {"a": Invite(3, 0, 1)}
        self.assertEqual(claim_invite(seen, dict(seen)), (None, seen))

    def test_an_invite_made_since_the_last_look_counts(self):
        code, _ = claim_invite({}, {"new": Invite(1, 0, 5)})
        self.assertEqual(code, "new")

    def test_a_used_up_single_use_invite_is_claimed(self):
        code, after = claim_invite({"once": Invite(0, 1, 5)}, {})
        self.assertEqual((code, after), ("once", {}))

    def test_a_deleted_invite_with_uses_left_is_not_claimed(self):
        self.assertEqual(claim_invite({"a": Invite(1, 10, 5)}, {})[0], None)

    def test_two_joins_at_once_each_find_their_invite(self):
        seen = {"a": Invite(0, 0, 1), "b": Invite(0, 0, 2)}
        now = {"a": Invite(1, 0, 1), "b": Invite(1, 0, 2)}
        first, seen = claim_invite(seen, now)
        second, _ = claim_invite(seen, now)
        self.assertEqual({first, second}, {"a", "b"})


if __name__ == "__main__":
    unittest.main()
