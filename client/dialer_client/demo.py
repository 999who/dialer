"""Scripted UI demo: shows every overlay state from the mockup without audio or a backend."""
from __future__ import annotations

import math
import random
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from . import theme as T
from .overlay import Overlay

HINTS = [
    {"id": "d1", "category": "info", "topic": "crm", "hint": "CRM od 1500 zł/mc + start od 2900 zł netto.",
     "quote": "A ile kosztuje taki CRM dla firmy na osiem osób?", "match": 0.91,
     "variants": ["CRM od 1500 zł/mc + start od 2900 zł netto.", "Dokładną wycenę podamy na bezpłatnej konsultacji."],
     "sources": [{"title": "Cennik: własny CRM", "ref": ""}]},
    {"id": "d2", "category": "objection", "topic": "cena",
     "hint": "Zacznij od jednego produktu; zero prowizji, konta zostają u klienta.",
     "quote": "Szczerze mówiąc, to dla nas trochę za drogo…", "match": 0.92,
     "variants": ["Zacznij od jednego produktu; zero prowizji, konta zostają u klienta.",
                  "Lepsze warunki od 6 miesięcy — szczegóły na konsultacji.",
                  "Zaproponuj bezpłatną konsultację 30–45 min."],
     "sources": [{"title": "Obiekcja: za drogo", "ref": ""}, {"title": "Skrypt: płatność w ratach", "ref": ""}]},
    {"id": "d3", "category": "warning", "topic": "",
     "hint": "Nie obiecuj liczb! Metodyka, optymalizacja co tydzień, raporty.", "quote": "", "match": None,
     "variants": [], "sources": []},
]
LINES = [(3, "operator", "Dzień dobry, EMANAGER, w czym mogę pomóc?"),
         (8, "client", "A ile kosztuje taki CRM dla firmy na osiem osób?"),
         (14, "operator", "Yyy… to zależy…"),
         (22, "client", "Szczerze mówiąc, to dla nas trochę za drogo…"),
         (30, "operator", "Gwarantuję, że będzie minimum 50 klientów miesięcznie.")]


def main() -> int:
    app = QApplication(sys.argv)
    T.load_fonts()
    app.setFont(T.sans(13))
    ov = Overlay(sys.argv[2] if len(sys.argv) > 2 else "bottom-right", hint_seconds=12)
    ov.show()
    t = {"s": 0.0}

    def tick():
        t["s"] += 0.1
        s = t["s"]
        ov.set_levels(abs(math.sin(s * 3)) * 0.08 * random.random(), abs(math.cos(s * 2)) * 0.1 * random.random())

    def second():
        s = int(t["s"])
        ov.set_call(1 <= s < 42, max(0, s - 1))
        for at, who, txt in LINES:
            if s == at:
                ov.add_transcript(at, who, txt)
        if s == 2:
            ov.show_client({"kind": "client", "via": "number", "title": "Pizzeria Osielsko",
                            "person": "Aldona (właścicielka)", "subscriber": True,
                            "hours": {"limit": 10, "left": 3.5, "used": 6.5, "pct": 65},
                            "ticket_items": [{"number": 212, "title": "Zmiana menu na stronie", "status": "w_toku"},
                                             {"number": 215, "title": "Faktura za wrzesień", "status": "nowe"}],
                            "last_call_at": "2026-10-01", "last_call_title": "Aktualizacja cennika dostaw",
                            "earlier_calls": [{"at": "2026-09-24", "title": "Reklamacja dostawy z 20.09"},
                                              {"at": "2026-09-10", "title": "Pytanie o fakturę za sierpień"}],
                            "promises": ["Wysłać podgląd nowego menu", "Oddzwonić w piątek"]})
        if s in (10, 24, 32):
            ov.show_hint(HINTS[(10, 24, 32).index(s)])
            ov.set_latency(random.randint(1500, 2300))
        if s == 36:
            ov.set_online(False, True)
            ov.show_error("server", "ponowna próba za 8 s")
        if s == 40:
            ov.set_online(True, True)
            ov.clear_error("server")
        if s == 42:
            ov.show_summary({"duration_s": 41, "summary": "Klient pyta o CRM dla 8 osób; uznał cenę za wysoką. "
                             "Następny krok: bezpłatna konsultacja.", "hints": 3, "used": 2, "objections": 1})
        if s == 48:
            ov.show_error("no_zadarma")

    QTimer(app, interval=100, timeout=tick).start()
    QTimer(app, interval=1000, timeout=second).start()
    return app.exec()
