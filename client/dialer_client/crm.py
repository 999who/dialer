"""EMANAGER CRM (Supabase) for the client card: operator sign-in and read-only lookups.

The operator signs in with their own CRM account (Supabase Auth, email + password), so every
query runs with their permissions: the CRM's row-level security decides what the dialer sees,
exactly as in the CRM itself. Nothing here writes to the CRM.

Who is calling, in this order (see `ClientCard`):
1. the number belongs to a CRM client: a client contact (`customer_contacts.phone_key`), the
   client's own phone (`clients.phone`) or a deal linked to a client (`crm_deals.client_id`);
2. otherwise a deal without a client, then the Google contacts import (`contacts.phone_key`);
3. the history of earlier calls from the same number works even when nothing above matched.
One number can belong to two clients (one owner, two companies): the card shows both.

Plain urllib, no extra dependency; every call blocks, so the app runs them in a thread.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import logging
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("crm")

TIMEOUT_S = 10
CACHE_TTL_S = 600            # clients and deals are re-read every 10 minutes
CLOSED_TICKETS = ("rozwiazane", "zamkniete")
TEST_NAMES = re.compile(r"\btest", re.I)
LEGAL = re.compile(r"\b(sp(o|ó)lka|sp|z|o|oo|s\.?a|sa|sc|s\.?c|ograniczona|odpowiedzialnoscia|"
                   r"odpowiedzialnością|komandytowa|jawna|ltd|gmbh|inc|firma|pphu|phu|fhu)\b", re.I)


class CrmError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def normalize_phone(raw: str | None) -> str:
    """Last 9 digits, the same as the CRM's normalize_phone(); '' when there aren't 9."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-9:] if len(digits) >= 9 else ""


def fold(s: str | None) -> str:
    """Lowercase without Polish diacritics, for name matching."""
    s = (s or "").replace("ł", "l").replace("Ł", "L")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


STOP = {"pan", "pani", "panie", "jest", "dla", "nie", "tak", "dzien", "dobry", "halo", "tutaj", "mowi",
        "imie", "nazywam", "sie", "jestem", "the", "and", "firmy", "dzwonie"}


def name_tokens(s: str | None) -> set[str]:
    words = re.findall(r"[a-z0-9]+", LEGAL.sub(" ", fold(s)))
    return {w for w in words if len(w) >= 3 and w not in STOP}


@dataclass
class Session:
    access_token: str
    refresh_token: str
    expires_at: float
    user_id: str
    email: str


def is_secret_key(key: str) -> bool:
    """A Supabase key that bypasses row-level security (sb_secret_… or a service_role JWT)."""
    key = (key or "").strip()
    if key.startswith("sb_secret_"):
        return True
    parts = key.split(".")
    if len(parts) == 3:
        try:
            pad = "=" * (-len(parts[1]) % 4)
            return json.loads(base64.urlsafe_b64decode(parts[1] + pad)).get("role") == "service_role"
        except (ValueError, binascii.Error):
            return False
    return False


class Supabase:
    """Minimal Supabase client: Auth (password, refresh) and PostgREST reads/RPC."""

    def __init__(self, url: str, key: str):
        if is_secret_key(key):  # it would skip the CRM's access rules for every operator
            raise CrmError("To jest klucz tajny (secret/service_role). Dialer przyjmuje tylko klucz publiczny.")
        self.url = url.rstrip("/")
        self.key = key
        self.session: Session | None = None
        self.on_session = None   # called with the new Session after sign-in and every refresh

    # ------------------------------------------------------------ http
    def _request(self, method: str, path: str, body: dict | None = None, auth: bool = True,
                 headers: dict | None = None):
        h = {"apikey": self.key, "Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            h["Authorization"] = f"Bearer {self.token()}"
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}")
            except ValueError:
                err = {}
            msg = err.get("msg") or err.get("message") or err.get("error_description") or str(e)
            raise CrmError(msg, e.code, str(err.get("code") or err.get("error_code") or "")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CrmError(f"brak połączenia z CRM: {getattr(e, 'reason', e)}") from None
        return json.loads(raw) if raw else None

    # ------------------------------------------------------------ auth
    def _set_session(self, d: dict) -> Session:
        user = d.get("user") or {}
        self.session = Session(d["access_token"], d["refresh_token"],
                               time.time() + float(d.get("expires_in", 3600)), user.get("id", ""),
                               user.get("email", ""))
        if self.on_session:
            self.on_session(self.session)  # refresh tokens are single-use: the app must keep the new one
        return self.session

    def sign_in(self, email: str, password: str) -> Session:
        d = self._request("POST", "/auth/v1/token?grant_type=password",
                          {"email": email, "password": password}, auth=False)
        return self._set_session(d)

    def refresh(self, refresh_token: str | None = None) -> Session:
        rt = refresh_token or (self.session.refresh_token if self.session else "")
        if not rt:
            raise CrmError("nie zalogowano", 401)
        d = self._request("POST", "/auth/v1/token?grant_type=refresh_token", {"refresh_token": rt}, auth=False)
        return self._set_session(d)

    def sign_out(self) -> None:
        if self.session:
            try:
                self._request("POST", "/auth/v1/logout")
            except CrmError:
                pass
        self.session = None

    def token(self) -> str:
        if not self.session:
            raise CrmError("nie zalogowano", 401)
        if self.session.expires_at - time.time() < 60:
            self.refresh()
        return self.session.access_token

    # ------------------------------------------------------------ data
    def select(self, table: str, **params) -> list[dict]:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, safe="*,.()")
        return self._request("GET", f"/rest/v1/{table}?{q}") or []

    def rpc(self, fn: str, args: dict | None = None):
        return self._request("POST", f"/rest/v1/rpc/{fn}", args or {})


# ---------------------------------------------------------------- the card
@dataclass
class ClientCard:
    """What the overlay shows and what Gemini gets. Built from whatever the operator may read."""
    title: str = ""                 # company, or the person when there is no company
    person: str = ""                # who is calling, when known
    kind: str = "unknown"           # client | lead | contact | unknown
    via: str = ""                   # number | name
    phone: str = ""
    client_ids: list[str] = field(default_factory=list)
    retainer: str = ""              # "Abonament: zostało 3.5 h z 10 h"
    tickets: list[str] = field(default_factory=list)
    deals: list[str] = field(default_factory=list)
    last_call: str = ""
    promised: str = ""
    callback: str = ""
    calls_count: int = 0
    summary: str = ""               # latest AI summary of the client, for Gemini only
    history: list[str] = field(default_factory=list)  # earlier calls, for Gemini only
    # the same facts, structured for the overlay card
    subscriber: bool = False        # has a retainer (ABONAMENT badge)
    hours: dict = field(default_factory=dict)   # {"limit", "used", "left", "pct"} from get_client_usage_banner
    ticket_items: list[dict] = field(default_factory=list)  # {"number", "title", "status"}
    last_call_at: str = ""          # ISO date of the last talked-through call
    last_call_title: str = ""
    promises: list[str] = field(default_factory=list)       # what was agreed, item by item
    lead_since: str = ""            # ISO date the deal was opened

    @property
    def found(self) -> bool:
        return bool(self.title or self.person or self.calls_count)

    def to_dict(self) -> dict:
        return {"title": self.title, "person": self.person, "kind": self.kind, "via": self.via,
                "retainer": self.retainer, "tickets": self.tickets, "deals": self.deals,
                "last_call": self.last_call, "promised": self.promised, "callback": self.callback,
                "calls_count": self.calls_count, "phone": self.phone, "subscriber": self.subscriber,
                "hours": self.hours, "ticket_items": self.ticket_items, "last_call_at": self.last_call_at,
                "last_call_title": self.last_call_title, "promises": self.promises, "lead_since": self.lead_since}

    def to_prompt(self) -> str:
        """The client block of the master prompt, in Polish."""
        if not self.found:
            return ("- Rozmówca nie został zidentyfikowany w CRM. Rozpoznaj go ze słuchu; "
                    "jeśli to nowy kontakt, stosuj scenariusz nowego leada.")
        kind = {"client": "Klient z CRM", "lead": "Lead (szansa sprzedaży, jeszcze nie klient)",
                "contact": "Kontakt z książki adresowej, nie klient w CRM"}.get(self.kind, "Nieznany")
        lines = []
        if self.person:
            lines.append(f"- Rozmówca: {self.person}")
        if self.title:
            lines.append(f"- Firma / klient: {self.title}")
        lines.append(f"- Status relacji: {kind} (rozpoznany po {'numerze' if self.via == 'number' else 'nazwie'})")
        if self.retainer:
            lines.append(f"- {self.retainer}")
        if self.tickets:
            lines.append("- Otwarte zgłoszenia: " + "; ".join(self.tickets))
        if self.deals:
            lines.append("- Otwarte szanse sprzedaży: " + "; ".join(self.deals))
        if self.callback:
            lines.append(f"- {self.callback}")
        if self.summary:
            lines.append(f"- Podsumowanie klienta (AI): {self.summary}")
        if self.history:
            lines.append("- Poprzednie rozmowy (najnowsze najpierw):")
            lines.extend(f"  • {h}" for h in self.history)
        return "\n".join(lines)


def _short(s: str | None, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _date(iso: str | None) -> str:
    return (iso or "")[:10]


def _num(x: float) -> str:
    return f"{x:g}"


def _retainer_line(h: dict) -> str:
    """Hours for Gemini. Over the limit it says so outright: the overrun moves to next month's package,
    and that is the moment to offer a bigger one."""
    over = h["used"] - h["limit"]
    if over > 0:
        return (f"Abonament {_num(h['limit'])} h/mies.: PRZEKROCZONY, wykorzystano {_num(h['used'])} h "
                f"({_num(round(over, 1))} h ponad limit). Nadwyżka przechodzi na następny miesiąc; "
                f"okazja, by zaproponować większy pakiet godzin")
    return (f"Abonament: zostało {_num(h['left'])} h z {_num(h['limit'])} h w tym miesiącu "
            f"({h['pct']}% wykorzystane)")


def _deal_line(d: dict) -> str:
    title = _short(d["title"], 90)
    if d.get("contract_value") and "zł" not in title:  # titles often carry the amount already
        title += f", {int(d['contract_value'])} zł"
    return title


def split_promises(s: str | None, n: int = 3) -> list[str]:
    """'1. Wysłać ofertę. 2. Oddzwonić w piątek.' -> ['Wysłać ofertę', 'Oddzwonić w piątek']."""
    s = " ".join((s or "").split())
    if not s:
        return []
    parts = re.split(r"(?:^|\s)\d{1,2}[.)]\s+", s)
    if len([p for p in parts if p.strip()]) < 2:
        s = re.sub(r"^\d{1,2}[.)]\s+", "", s)
        parts = re.split(r"(?<=[.!?])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ])", s)
    return [_short(p.strip().rstrip("."), 90) for p in parts if p.strip()][:n]


class Crm:
    """Lookups for the card. Small tables are cached (clients, deals, contact persons) because
    their phone formats vary ("+48 501 135 008", "501135008") and are matched here."""

    def __init__(self, sb: Supabase, own_numbers: list[str] | None = None):
        self.sb = sb
        self.own = {normalize_phone(n) for n in (own_numbers or []) if normalize_phone(n)}
        self._cache_at: float | None = None   # not 0.0: monotonic() can be small right after boot
        self.clients: dict[str, dict] = {}
        self.deals: list[dict] = []
        self.persons: list[dict] = []
        self.has_current_call_fn = True
        self.profile: dict = {}

    # ------------------------------------------------------------ cache
    def refresh_cache(self, force: bool = False) -> None:
        if not force and self._cache_at is not None and time.monotonic() - self._cache_at < CACHE_TTL_S:
            return
        rows = self.sb.select("clients", select="id,name,phone,status,has_retainer,monthly_hours_limit,is_demo")
        self.clients = {r["id"]: r for r in rows if not r.get("is_demo") and not TEST_NAMES.search(r["name"] or "")}
        try:
            self.deals = self.sb.select(
                "crm_deals", select="id,title,client_id,contact_phone,deal_status,contract_value,"
                "next_contact_date,next_contact_note,created_at", is_archived="eq.false", is_demo="eq.false")
        except CrmError as e:  # no access to the sales pipeline: the card works without deals
            log.info("deals unavailable: %s", e)
            self.deals = []
        self.persons = self.sb.select("customer_contacts", select="first_name,last_name,position,customer_id,"
                                      "phone_key,is_primary", is_demo="eq.false")
        self._cache_at = time.monotonic()
        log.info("crm cache: %d clients, %d deals, %d contact persons",
                 len(self.clients), len(self.deals), len(self.persons))

    def load_profile(self) -> dict:
        if self.sb.session:
            rows = self.sb.select("profiles", select="id,full_name,email,zadarma_sip_login",
                                  id=f"eq.{self.sb.session.user_id}")
            self.profile = rows[0] if rows else {}
        return self.profile

    # ------------------------------------------------------------ the current call's number
    def current_call(self, internal: str = "") -> dict | None:
        """Number of the call that just started, from Zadarma's webhook events in the CRM.

        Needs the optional dialer_current_call() function (backend/sql/crm_dialer_current_call.sql);
        without it the card falls back to recognising the caller by name.
        """
        if not self.has_current_call_fn:
            return None
        try:
            rows = self.sb.rpc("dialer_current_call", {"_internal": internal or None})
        except CrmError as e:
            if e.status == 404 or e.code in ("PGRST202", "42883"):
                log.info("dialer_current_call() is not installed in the CRM: caller ID by name only")
                self.has_current_call_fn = False
                return None
            raise
        return rows[0] if rows else None

    # ------------------------------------------------------------ lookups
    def by_phone(self, phone: str) -> ClientCard:
        n = normalize_phone(phone)
        card = ClientCard(phone=phone, via="number")
        if not n or n in self.own:
            return card
        self.refresh_cache()
        persons = [p for p in self.persons if p.get("phone_key") == n and p["customer_id"] in self.clients]
        ids = [p["customer_id"] for p in persons]
        ids += [cid for cid, c in self.clients.items() if normalize_phone(c.get("phone")) == n]
        deals = [d for d in self.deals if normalize_phone(d.get("contact_phone")) == n]
        ids += [d["client_id"] for d in deals if d.get("client_id")]
        ids = [i for i in dict.fromkeys(ids) if i in self.clients]  # unique, known, not test/demo
        if persons:
            p = next((p for p in persons if p["customer_id"] in ids), persons[0])
            card.person = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
            if p.get("position"):
                card.person += f" ({p['position']})"
        if ids:
            card.kind = "client"
            card.client_ids = ids[:2]
            card.title = " / ".join(self.clients[i]["name"] for i in card.client_ids)
        elif deals:
            card.kind = "lead"
            card.title = deals[0]["title"]
            card.lead_since = _date(deals[0].get("created_at"))
        else:
            contacts = self.sb.select("contacts", select="first_name,last_name,organization",
                                      phone_key=f"eq.{n}", limit="5")
            if contacts:
                card.kind = "contact"
                orgs = list(dict.fromkeys(c["organization"].strip() for c in contacts if (c.get("organization") or "").strip()))
                names = list(dict.fromkeys(" ".join(x for x in (c.get("first_name"), c.get("last_name")) if x).strip()
                                           for c in contacts))
                card.title = " / ".join(orgs[:2])
                if len(names) == 1 or not orgs:
                    card.person = card.person or (names[0] if len(names) == 1 else
                                                  "Kilka kontaktów: " + ", ".join(names[:3]))
        self._fill(card, deals, n)
        return card

    def by_name(self, heard: str) -> ClientCard:
        """The caller said who they are ("Marek z Bipromaszu"): best client or open deal by name."""
        card = ClientCard(via="name")
        heard_t = name_tokens(heard)
        if not heard_t:
            return card
        self.refresh_cache()

        def score(name: str | None) -> int:
            t = name_tokens(name)
            return sum(1 for w in heard_t if any(w[:5] == x[:5] for x in t))  # "bipromaszu" ~ "bipromasz"

        best = max(self.clients.values(), key=lambda c: score(c["name"]), default=None)
        if best and score(best["name"]) > 0:
            card.kind, card.title, card.client_ids = "client", best["name"], [best["id"]]
            self._fill(card, [], "")
            return card
        deal = max(self.deals, key=lambda d: score(d["title"]), default=None)
        if deal and score(deal["title"]) > 0:
            card.kind, card.title, card.lead_since = "lead", deal["title"], _date(deal.get("created_at"))
            self._fill(card, [deal], normalize_phone(deal.get("contact_phone")))
        return card

    # ------------------------------------------------------------ details
    def _fill(self, card: ClientCard, deals: list[dict], n: str) -> None:
        ids = card.client_ids
        open_deals = deals + [d for d in self.deals if d.get("client_id") in ids and d not in deals]
        card.deals = [_deal_line(d) for d in open_deals[:3]]
        nxt = next((d for d in open_deals if d.get("next_contact_date")), None)
        if nxt:
            day = _date(nxt["next_contact_date"])
            what = "Zaległy kontakt, planowany" if day < dt.date.today().isoformat() else "Następny kontakt"
            card.callback = f"{what} {day}: {_short(nxt.get('next_contact_note'), 60)}"
        for cid in ids:
            c = self.clients[cid]
            card.subscriber = card.subscriber or bool(c.get("has_retainer") or c.get("monthly_hours_limit"))
            if c.get("monthly_hours_limit"):
                try:
                    u = self.sb.rpc("get_client_usage_banner", {"_client_id": cid}) or {}
                    if u.get("limit_godzin") and not card.hours:
                        card.hours = {"limit": float(u["limit_godzin"]), "left": float(u.get("zostalo_godzin") or 0),
                                      "used": float(u.get("zuzyte_godzin") or 0), "pct": int(u.get("procent") or 0)}
                        card.retainer = _retainer_line(card.hours)
                except CrmError as e:
                    log.info("usage unavailable: %s", e)
            elif c.get("has_retainer") and not card.retainer:
                card.retainer = "Klient abonamentowy"
            try:
                rows = self.sb.select("tickets", select="ticket_number,title,status,last_message_at",
                                      client_id=f"eq.{cid}", deleted_at="is.null",
                                      status=f"not.in.({','.join(CLOSED_TICKETS)})",
                                      order="last_message_at.desc.nullslast", limit="3")
                card.tickets += [f"#{r['ticket_number']} {_short(r['title'], 50)} ({r['status'].replace('_', ' ')})"
                                 for r in rows]
                card.ticket_items += [{"number": r["ticket_number"], "title": r["title"] or "", "status": r["status"]}
                                      for r in rows]
            except CrmError as e:
                log.info("tickets unavailable: %s", e)
            try:
                rows = self.sb.select("client_ai_summaries", select="content", client_id=f"eq.{cid}",
                                      order="created_at.desc", limit="1")
                if rows:
                    card.summary = _short(rows[0]["content"], 600)
            except CrmError:
                pass
        self._calls(card, n)

    def _calls(self, card: ClientCard, n: str) -> None:
        conds = [f"client_id.eq.{i}" for i in card.client_ids]
        if n:
            conds.append(f"client_number.like.*{n}")
        if not conds:
            return
        try:
            rows = self.sb.select("calls", select="called_at,direction,title,ai_summary,suggestions,callback_status",
                                  **{"or": f"({','.join(conds)})"}, deleted_at="is.null",
                                  order="called_at.desc.nullslast", limit="8")
        except CrmError as e:
            log.info("calls unavailable: %s", e)
            return
        card.calls_count = len(rows)
        talked = [r for r in rows if r.get("ai_summary")]
        if talked:
            last = talked[0]
            card.last_call = f"{_date(last['called_at'])}: {_short(last.get('title') or last['ai_summary'], 70)}"
            card.promised = _short(last.get("suggestions"), 140)
            card.last_call_at = _date(last["called_at"])
            card.last_call_title = _short(last.get("title") or last["ai_summary"], 70)
            card.promises = split_promises(last.get("suggestions"))
        if any(r.get("callback_status") == "not_started" for r in rows) and not card.callback:
            card.callback = "Nieoddzwoniony nieodebrany telefon"
        card.history = [f"{_date(r['called_at'])} ({'przych.' if r.get('direction') == 'inbound' else 'wych.'}): "
                        f"{_short(r.get('title'), 80)}. {_short(r.get('ai_summary'), 300)} "
                        f"Ustalenia: {_short(r.get('suggestions'), 200)}" for r in talked[:5]]
