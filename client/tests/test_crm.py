import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dialer_client.crm import Crm, CrmError, name_tokens, normalize_phone  # noqa: E402

CLIENTS = [
    {"id": "c1", "name": "Floresca", "phone": "+48 509 506 219", "status": "active", "has_retainer": True,
     "monthly_hours_limit": 10, "is_demo": False},
    {"id": "c2", "name": "FIRFAS RAFAŁ \"FIRFEK\"", "phone": None, "status": "active", "has_retainer": False,
     "monthly_hours_limit": None, "is_demo": False},
    {"id": "c3", "name": "Squashpoint", "phone": None, "status": "active", "has_retainer": False,
     "monthly_hours_limit": None, "is_demo": False},
    {"id": "t1", "name": "testowaaaa", "phone": "123456789", "status": "active", "has_retainer": False,
     "monthly_hours_limit": None, "is_demo": False},
    {"id": "c4", "name": "AJM POLSKA SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ", "phone": None, "status": "x",
     "has_retainer": False, "monthly_hours_limit": None, "is_demo": False},
]
DEALS = [
    {"id": "d1", "title": "Sklepy cmentarne", "client_id": None, "contact_phone": "509506219",
     "deal_status": "open", "contract_value": None, "next_contact_date": None, "next_contact_note": None},
    {"id": "d2", "title": "Squash Point kampanie", "client_id": "c3", "contact_phone": "+48 783 068 607",
     "deal_status": "open", "contract_value": 1500, "next_contact_date": "2026-10-10T09:00:00+00:00",
     "next_contact_note": "Wysłać raport"},
    {"id": "d3", "title": "Soforek.pl - Dorota Leśniewska", "client_id": None, "contact_phone": "606 833 032",
     "deal_status": "open", "contract_value": None, "next_contact_date": None, "next_contact_note": None},
]
PERSONS = [
    {"first_name": "Filip", "last_name": "K", "position": None, "customer_id": "c1", "phone_key": "509506219"},
    {"first_name": "Monika", "last_name": "R", "position": "biuro", "customer_id": "c2", "phone_key": "783068607"},
    {"first_name": "Test", "last_name": "T", "position": None, "customer_id": "t1", "phone_key": "123456789"},
    {"first_name": "Karol", "last_name": "B", "position": "prezes", "customer_id": "c4", "phone_key": "791391354"},
]


class FakeSupabase:
    session = None

    def __init__(self):
        self.calls = []

    def select(self, table, **params):
        self.calls.append((table, params))
        if table == "clients":
            return CLIENTS
        if table == "crm_deals":
            return DEALS
        if table == "customer_contacts":
            return PERSONS
        if table == "contacts":
            return [{"first_name": "Jan", "last_name": "Nowak", "organization": "Laser Groot"}] \
                if params.get("phone_key") == "eq.698551380" else []
        if table == "tickets":
            return [{"ticket_number": 7, "title": "Strona nie działa", "status": "w_toku", "last_message_at": None}] \
                if params.get("client_id") == "eq.c1" else []
        if table == "calls":
            return [{"called_at": "2026-10-01T10:00:00+00:00", "direction": "inbound", "title": "Pytanie o sklep",
                     "ai_summary": "Klient pytał o sklep.", "suggestions": "1. Wysłać ofertę.",
                     "callback_status": None}]
        return []

    def rpc(self, fn, args=None):
        self.calls.append((fn, args))
        if fn == "get_client_usage_banner":
            return {"limit_godzin": 10, "zostalo_godzin": 3.5, "procent": 65}
        if fn == "dialer_current_call":
            raise CrmError("Could not find the function", 404, "PGRST202")
        return None


def crm():
    return Crm(FakeSupabase(), ["609037902"])


def test_normalize_phone_matches_crm():
    assert normalize_phone("+48 509 506 219") == "509506219"
    assert normalize_phone("48509506219") == "509506219"
    assert normalize_phone("100") == ""
    assert normalize_phone(None) == ""


def test_client_by_contact_and_own_phone_with_details():
    card = crm().by_phone("+48509506219")
    assert card.kind == "client" and card.title == "Floresca" and card.person == "Filip K"
    assert card.retainer.startswith("Abonament: zostało 3.5 h z 10 h")
    assert card.tickets == ["#7 Strona nie działa (w toku)"]
    assert card.deals[0].startswith("Sklepy cmentarne")       # the deal without a client rides along
    assert card.last_call.startswith("2026-10-01") and card.promised == "1. Wysłać ofertę."
    prompt = card.to_prompt()
    assert "Firma / klient: Floresca" in prompt and "Poprzednie rozmowy" in prompt


def test_one_number_two_companies_shows_both():
    card = crm().by_phone("783068607")
    assert card.kind == "client" and card.title == 'FIRFAS RAFAŁ "FIRFEK" / Squashpoint'
    assert card.callback.startswith("Następny kontakt 2026-10-10")


def test_test_clients_and_own_numbers_are_ignored():
    c = crm()
    card = c.by_phone("123456789")
    assert card.kind != "client" and "testowaaaa" not in card.title and card.person == ""
    c = crm()
    own = c.by_phone("+48 609 037 902")
    assert not own.found and not c.sb.calls  # the company's own number: no lookups at all


def test_lead_and_google_contact():
    assert crm().by_phone("606833032").kind == "lead"
    card = crm().by_phone("698551380")
    assert card.kind == "contact" and card.title == "Laser Groot" and card.person == "Jan Nowak"


def test_by_name():
    assert name_tokens("AJM POLSKA SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ") == {"ajm", "polska"}
    card = crm().by_name("Rafał z Firfeka")
    assert card.kind == "client" and card.title.startswith("FIRFAS") and card.via == "name"
    assert crm().by_name("Dorota z Soforka").kind == "lead"
    assert not crm().by_name("Pan Kowalski").found


def test_current_call_without_the_crm_function_falls_back_quietly():
    c = crm()
    assert c.current_call("100") is None and c.has_current_call_fn is False
    assert c.current_call("100") is None  # not asked again


def test_unknown_number_still_brings_its_call_history():
    card = crm().by_phone("500000000")
    assert card.kind == "unknown" and card.calls_count == 1
    assert "Poprzednie rozmowy" in card.to_prompt()
