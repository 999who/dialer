"""The call queue: who to call between calls, built read-only from the CRM."""
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dialer_client.crm import Crm, CrmError, dial_uri  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def ago(**kw) -> str:
    return (NOW - dt.timedelta(**kw)).isoformat()


def local_today_at(h: int, m: int) -> str:
    d = NOW.astimezone().replace(hour=h, minute=m, second=0)
    return d.astimezone(dt.timezone.utc).isoformat()


CLIENTS = [{"id": "c1", "name": "Floresca", "phone": None, "city": "Łódź", "status": "active", "has_retainer": True,
            "monthly_hours_limit": 10, "is_demo": False}]
PERSONS = [{"first_name": "Filip", "last_name": "K", "position": "właściciel", "customer_id": "c1",
            "phone_key": "512000204", "is_primary": True}]
COLUMNS = [{"id": "k1", "name": "LEAD – do przesiania", "lexo_rank": "B"},
           {"id": "k2", "name": "KWALIFIKACJA", "lexo_rank": "C"},
           {"id": "k3", "name": "OFERTA", "lexo_rank": "F"},
           {"id": "kx", "name": "Mikro interwencje", "lexo_rank": "b"}]
PLACES = [{"id": "p1", "name": "Uśmiech Plus", "phone_number": "+48 42 000 15 90", "category": "Stomatolog",
           "rating": 4.8, "reviews_count": 126, "address": "ul. Piotrkowska 112, Łódź", "website": "https://x.pl"}]


def deals():
    soon = (NOW - dt.timedelta(minutes=1)).isoformat()
    return [
        {"id": "d_now", "title": "Kwiatownia – Marta", "client_id": None, "contact_phone": "698 000 551",
         "contact_email": None, "contract_value": 12400, "next_contact_date": soon, "next_contact_done_at": None,
         "next_contact_note": "Zapytać o decyzję", "column_id": "k3", "source": "website",
         "offer_sent_at": ago(days=5), "created_at": ago(days=30)},
        {"id": "d_mail", "title": "Biuro Rachunkowe Lis – Adam", "client_id": None, "contact_phone": None,
         "contact_email": "adam@example.com", "contract_value": 8900, "next_contact_date": ago(days=2),
         "next_contact_done_at": None, "next_contact_note": None, "column_id": "k2", "source": "manual",
         "offer_sent_at": None, "created_at": ago(days=30)},
        {"id": "d_old", "title": "Stara sprawa", "client_id": None, "contact_phone": "600 000 001",
         "contract_value": None, "next_contact_date": ago(days=45), "next_contact_done_at": None,
         "column_id": "k1", "source": None, "offer_sent_at": None, "created_at": ago(days=90)},
        {"id": "d_done", "title": "Zrobione", "client_id": None, "contact_phone": "600 000 002",
         "contract_value": None, "next_contact_date": ago(hours=1), "next_contact_done_at": ago(minutes=30),
         "column_id": "k1", "source": None, "offer_sent_at": None, "created_at": ago(days=9)},
        {"id": "d_dup", "title": "Ten sam numer co nieodebrane", "client_id": None, "contact_phone": "601000318",
         "contract_value": None, "next_contact_date": ago(days=1), "next_contact_done_at": None,
         "column_id": "k1", "source": None, "offer_sent_at": None, "created_at": ago(days=9)},
    ]


RECENT = [  # newest first, as the CRM returns them
    {"client_number": "48601000318", "called_at": ago(minutes=20), "direction": "inbound", "disposition": "cancel"},
    {"client_number": "48512000204", "called_at": ago(hours=2), "direction": "inbound", "disposition": "no answer"},
    {"client_number": "48420001590", "called_at": ago(hours=3), "direction": "inbound", "disposition": "cancel"},
    {"client_number": "48507000870", "called_at": ago(hours=4), "direction": "inbound", "disposition": "busy"},
    {"client_number": "48733000102", "called_at": ago(hours=5), "direction": "inbound", "disposition": "cancel"},
    {"client_number": "48512000204", "called_at": ago(hours=20), "direction": "inbound", "disposition": "cancel"},
    {"client_number": "48791000000", "called_at": ago(hours=21), "direction": "inbound", "disposition": "cancel"},
    # answered after being missed: not in the queue
    {"client_number": "48700700700", "called_at": ago(hours=1), "direction": "outbound", "disposition": "answered"},
    {"client_number": "48700700700", "called_at": ago(hours=6), "direction": "inbound", "disposition": "cancel"},
    # callback marked done in the CRM
    {"client_number": "48700700701", "called_at": ago(hours=7), "direction": "inbound", "disposition": "cancel",
     "callback_status": "done"},
    # our own number
    {"client_number": "48609037902", "called_at": ago(hours=8), "direction": "inbound", "disposition": "cancel"},
    # we tried to call them back but nobody picked up: still missed
    {"client_number": "48420001590", "called_at": ago(days=2), "direction": "outbound", "disposition": "no answer"},
]
HISTORY = [
    {"client_number": "48512000204", "client_id": "c1", "called_at": ago(days=3), "direction": "inbound",
     "duration": 200, "title": "Pytanie o sklep", "ai_summary": "Klient pytał o sklep."},
    {"client_number": "48733000102", "client_id": None, "called_at": ago(days=10), "direction": "inbound",
     "duration": 120, "title": "Reklamacja dostawy", "ai_summary": "Reklamacja."},
    {"client_number": "48420001590", "client_id": None, "called_at": ago(days=7), "direction": "outbound",
     "duration": 15, "title": None, "ai_summary": None},
]


class FakeSupabase:
    session = None

    def __init__(self, deny=()):
        self.deny = set(deny)
        self.queries = []

    def select(self, table, **params):
        self.queries.append((table, params))
        if table in self.deny:
            raise CrmError("permission denied", 403)
        if table == "clients":
            return CLIENTS
        if table == "crm_deals":
            return deals()
        if table == "customer_contacts":
            return PERSONS
        if table == "crm_columns":
            return COLUMNS
        if table == "scraped_places":
            return PLACES
        if table == "contacts":
            return [{"first_name": "Marek", "last_name": "Zieliński", "organization": None,
                     "email": "marek.z@example.com", "phone_key": "507000870"}]
        if table == "calls":
            return HISTORY if "or" in params else RECENT
        if table == "tickets":
            return [{"client_id": "c1"}, {"client_id": "c1"}]
        if table == "deal_share_links":
            return [{"deal_id": "d_now", "link_open_count": 3, "is_revoked": False}]
        return []

    def rpc(self, fn, args=None):
        if fn == "get_client_usage_banner":
            return {"limit_godzin": 10, "zostalo_godzin": 3.5, "zuzyte_godzin": 6.5, "procent": 65}
        return None


def build(**kw):
    return Crm(FakeSupabase(**kw), ["609037902"]).queue(NOW)


def test_order_planned_now_then_missed_newest_first_then_overdue():
    q = build()
    keys = [i["key"] for i in q]
    assert keys[0] == "deal:d_now"
    assert keys[1:6] == ["missed:601000318", "missed:512000204", "missed:420001590", "missed:507000870",
                         "missed:733000102"]
    assert keys[6] == "missed:791000000"
    assert keys[7:] == ["deal:d_mail"]  # overdue 2 days; the one overdue 45 days and the done one are left out


def test_missed_drops_out_after_a_conversation_or_a_done_callback_and_own_numbers():
    keys = {i["key"] for i in build()}
    assert not keys & {"missed:700700700", "missed:700700701", "missed:609037902"}


def test_a_number_both_planned_and_missed_shows_once():
    q = build()
    assert sum(1 for i in q if "601000318" in i["phone"].replace(" ", "")) == 1


def test_every_type_gets_what_the_design_shows():
    q = {i["key"]: i for i in build()}
    lead = q["deal:d_now"]
    assert lead["type"] == "lead" and lead["reason"] == "now" and lead["value"] == 12400
    assert lead["stage"] == {"name": "OFERTA", "index": 3, "total": 4} and lead["offer_opens"] == 3
    assert lead["source"] == "formularz WWW" and lead["next_note"] == "Zapytać o decyzję"

    client = q["missed:512000204"]
    assert client["type"] == "client" and client["count"] == 2 and client["title"] == "Floresca"
    assert client["person"] == "Filip K" and client["position"] == "właściciel" and client["city"] == "Łódź"
    assert client["hours"]["left"] == 3.5 and client["tickets_open"] == 2
    assert client["calls"][0]["title"] == "Pytanie o sklep"

    maps = q["missed:420001590"]
    assert maps["type"] == "maps" and maps["calls_back"] and maps["rating"] == 4.8 and maps["reviews"] == 126
    assert maps["we_called_at"]  # our cold call (too short to have a summary) still counts

    google = q["missed:507000870"]
    assert google["type"] == "google" and google["title"] == "Marek Zieliński" and google["email"]

    assert q["missed:733000102"]["type"] == "hist"
    assert q["missed:791000000"]["type"] == "new"
    dup = q["missed:601000318"]  # missed, and the number has a deal: shown as the lead, once
    assert dup["type"] == "lead" and dup["reason"] == "missed" and dup["title"] == "Ten sam numer co nieodebrane"

    nophone = q["deal:d_mail"]
    assert nophone["phone"] == "" and nophone["email"] == "adam@example.com" and nophone["reason"] == "overdue"


def test_queue_works_without_access_to_deals_pipeline_and_maps():
    q = build(deny={"crm_deals", "crm_columns", "scraped_places", "deal_share_links"})
    assert q and all(i["key"].startswith("missed:") for i in q)
    assert {i["key"]: i for i in q}["missed:420001590"]["type"] in ("new", "hist")


def test_dial_uri():
    assert dial_uri("+48 509 506 219") == "tel:+48509506219"
    assert dial_uri("48509506219") == "tel:+48509506219"
    assert dial_uri("509506219") == "tel:+48509506219"
    assert dial_uri("") == ""


def test_queue_widget_rows_unfold_and_skip():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    from dialer_client.overlay import Overlay
    from dialer_client.widgets import QueueCard, queue_reason, queue_time

    app = QApplication.instance() or QApplication([])  # noqa: F841
    items = build()
    ov = Overlay()
    dialled, skipped = [], []
    ov.dial.connect(dialled.append)
    ov.queue_skip.connect(skipped.append)
    ov.show_queue(items + [dict(items[1], key=f"x{i}") for i in range(3)])
    card = next(c for c in ov.cards if isinstance(c, QueueCard))
    assert len(card.rows) == 6
    first = card.rows[0]
    assert first.card.isHidden() and not first.row.isHidden()
    first.set_open(True)
    assert first.row.isHidden() and not first.card.isHidden()
    first.dial.emit(first.uri)
    first.skip.emit(first.it["key"])
    assert dialled == ["tel:+48698000551"] and skipped == ["deal:d_now"]
    assert queue_time(items[0]) == "teraz"
    assert queue_reason(items[2]).startswith("Nieodebrane 2× · ")
    ov.show_client({"kind": "client", "title": "Floresca"})  # a call starts: the card replaces the queue
    assert not any(isinstance(c, QueueCard) for c in ov.cards)
