import unittest

from pollnews.card import render
from pollnews.extract import cards_from
from pollnews.feeds import build_polls
from pollnews.meta import parse_meta
from pollnews.nyt import card_for

HEAD = ("race_code,race_type,state,district,dem_id,dem_name,dem_party,rep_id,rep_name,rep_party,"
        "ind_id,ind_name,ind_party,ind_caucus,ind_party_label,dem2_id,rep2_id,rcv,rcv_anchor,"
        "threeway,ind_frac,same_party,cand2_name,cand1_inc,incumbent_party,seat_holder,"
        "dem_status,rep_status,ind_status\n")
RACES = HEAD + (
    "NE-SEN,senate,NE,SEN,C-1034,(No Democrat),DEM,C-0043,Pete Ricketts,REP,C-0044,Dan Osborn,"
    "IND,DEM,Independent,,,False,,True,0.85,,,False,REP,REP,,confirmed,confirmed\n"
    "CA-06,house,CA,06,C-0196,Richard Pan,DEM,C-0197,Kevin Kiley,IND,,,,,,,,False,,False,0.0,,,"
    "False,IND,IND,confirmed,confirmed,\n"
    "AR-SEN,senate,AR,SEN,C-1,Hallie Shoffner,DEM,C-2,Tom Cotton,REP,C-3,Jeff Wadlin,LIB,IND,"
    "Libertarian,,,False,,False,0.0,,,False,REP,REP,confirmed,confirmed,confirmed\n")
META = parse_meta(None, RACES)

COLS = ("poll_id,question_id,pollster,display_name,sponsors,partisan,start_date,end_date,"
        "population,sample_size,office_type,state,seat_number,stage,party,candidate_name,"
        "candidate_id,pct,url\n")


def feed(office, state, seat, rows):
    return COLS + "".join(
        f"p1,q1,GBAO,GBAO,,,10/18/26,10/22/26,lv,800,{office},{state},{seat},general,"
        f"{party},{name},{name},{pct},https://example.com\n" for party, name, pct in rows)


def only_card(feeds):
    (poll,) = build_polls(feeds).values()
    (key,) = poll.races
    return card_for(poll, key, META)


class IndependentTests(unittest.TestCase):
    def test_meta_reads_every_slot(self):
        self.assertEqual(META.incumbent("CA-06"), "Kevin Kiley")   # IND in the rep slot
        self.assertEqual(META.incumbent("NE-SEN"), "Pete Ricketts")
        self.assertEqual(META.independents_in("NE-SEN"), ["Dan Osborn"])
        self.assertEqual(META.independents_in("CA-06"), ["Kevin Kiley"])
        self.assertEqual(META.independents_in("AR-SEN"), [])        # a Libertarian isn't one

    def test_nyt_dem_coded_independent_shows_as_i(self):
        c = only_card({"senate": feed("U.S. Senate", "NE", "", [
            ("REP", "Pete Ricketts", 45), ("DEM", "Dan Osborn", 46), ("NONE", "Don't know", 7)])})
        text = render(c)
        self.assertIn("#NESen POLL", text)
        self.assertIn("Osborn (I) 🟠: 46%", text)
        self.assertIn("Ricketts (R, incumbent) 🔴: 45%", text)
        self.assertNotIn("(D)", text)

    def test_independent_incumbent_house(self):
        c = only_card({"house": feed("U.S. House", "CA", "6", [
            ("DEM", "Richard Pan", 36.7), ("IND", "Kevin Kiley", 36.5)])})
        text = render(c)
        self.assertIn("#CA6 POLL", text)
        self.assertIn("Pan (D) 🔵: 36.7%", text)
        self.assertIn("Kiley (I, incumbent) 🟠: 36.5%", text)

    def test_npa_is_independent(self):
        c = only_card({"house": feed("U.S. House", "CA", "6", [
            ("DEM", "Richard Pan", 40), ("NPA", "Kevin Kiley", 38)])})
        self.assertIn("Kiley (I, incumbent) 🟠: 38%", render(c))

    def test_poll_command_uses_ballot_party_too(self):
        data = {"found": True, "pollster": "GBAO", "races": [{
            "office": "SEN", "state": "NE", "population": "lv", "sample_size": 800,
            "questions": [{"answers": [
                {"name": "Dan Osborn", "party": "DEM", "pct": 46, "incumbent": False},
                {"name": "Pete Ricketts", "party": "REP", "pct": 45, "incumbent": True}]}]}]}
        (c,) = cards_from(data, META, "https://example.com")
        self.assertIn("Osborn (I) 🟠: 46%", render(c))


if __name__ == "__main__":
    unittest.main()
