import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dialer_client.crm import Crm, CrmError, _deal_line, _retainer_line, name_tokens, normalize_phone, split_promises  # noqa: E402

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
     "deal_status": "open", "contract_value": 1500, "next_contact_date": "2099-10-10T09:00:00+00:00",
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
            return {"limit_godzin": 10, "zostalo_godzin": 3.5, "zuzyte_godzin": 6.5, "procent": 65}
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
    assert card.subscriber and card.hours == {"limit": 10.0, "left": 3.5, "used": 6.5, "pct": 65}
    assert card.ticket_items == [{"number": 7, "title": "Strona nie działa", "status": "w_toku"}]
    assert card.last_call_at == "2026-10-01" and card.promises == ["Wysłać ofertę"]
    prompt = card.to_prompt()
    assert "Firma / klient: Floresca" in prompt and "Poprzednie rozmowy" in prompt


def test_one_number_two_companies_shows_both():
    card = crm().by_phone("783068607")
    assert card.kind == "client" and card.title == 'FIRFAS RAFAŁ "FIRFEK" / Squashpoint'
    assert card.callback.startswith("Następny kontakt 2099-10-10")


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


def test_split_promises():
    assert split_promises("1. Wysłać ofertę. 2. Oddzwonić w piątek.") == ["Wysłać ofertę", "Oddzwonić w piątek"]
    assert split_promises("Klient ustali datę. Należy pamiętać o promocji.") == [
        "Klient ustali datę", "Należy pamiętać o promocji"]
    assert split_promises(None) == []


def test_secret_keys_are_refused():
    import base64
    import json

    from dialer_client.crm import Supabase, is_secret_key

    def jwt(role):
        body = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJIUzI1NiJ9.{body}.sig"

    assert is_secret_key("sb_secret_abc") and is_secret_key(jwt("service_role"))
    assert not is_secret_key("sb_publishable_abc") and not is_secret_key(jwt("anon"))
    try:
        Supabase("https://x.supabase.co", jwt("service_role"))
    except CrmError:
        pass
    else:
        raise AssertionError("a service_role key must be refused")


def test_crm_address_is_built_in(tmp_path):
    from dialer_client.config import CRM_KEY, CRM_URL, load_config
    from dialer_client.crm import is_secret_key

    assert CRM_URL.startswith("https://") and CRM_KEY.startswith("sb_publishable_") and not is_secret_key(CRM_KEY)
    cfg = tmp_path / "config.toml"
    cfg.write_text('crm_url = ""\ncrm_key = ""\n')      # old config files have the lines empty
    c = load_config(cfg)
    assert (c.crm_url, c.crm_key) == (CRM_URL, CRM_KEY)
    cfg.write_text('crm_url = "https://other.supabase.co/"\ncrm_key = "sb_publishable_x"\n')
    assert load_config(cfg).crm_url == "https://other.supabase.co"


def test_prompt_lines_for_hours_over_limit_and_overdue_contact():
    over = _retainer_line({"limit": 10.0, "used": 20.4, "left": 0.0, "pct": 204})
    assert "PRZEKROCZONY" in over and "10.4 h ponad limit" in over and "po wycenie" in over
    assert _retainer_line({"limit": 10.0, "used": 6.5, "left": 3.5, "pct": 65}).startswith(
        "Abonament: zostało 3.5 h z 10 h")
    assert _deal_line({"title": "Kiosk na hali (29 000 zł)", "contract_value": 29000}) == "Kiosk na hali (29 000 zł)"
    assert _deal_line({"title": "Sklep", "contract_value": 1500}) == "Sklep, 1500 zł"
    c = crm()
    c.refresh_cache()
    c.deals = [dict(d, next_contact_date="2020-01-01T09:00:00+00:00") for d in c.deals]  # copies: DEALS is shared
    card = c.by_phone("783068607")
    assert card.callback.startswith("Zaległy kontakt, planowany 2020-01-01")
