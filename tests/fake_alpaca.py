"""In-memory stand-in for Alpaca's REST API, served through httpx.MockTransport."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx

PAPER = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"


class FakeAlpaca:
    def __init__(self) -> None:
        self.calendar: list[dict] = []
        self.bars: dict[str, list[dict]] = {}  # symbol -> rows in Alpaca's JSON shape
        self.page_size = 10_000
        self.fail_next: list[int] = []  # status codes to return before succeeding
        self.is_open = False
        self.requests: list[httpx.Request] = []

    def add_minutes(self, symbol: str, start: datetime, count: int, skip: set[int] = frozenset()) -> None:
        rows = self.bars.setdefault(symbol, [])
        for i in range(count):
            if i in skip:
                continue
            t = start + timedelta(minutes=i)
            rows.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100, "n": 3, "vw": 1.2})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), headers={"Retry-After": "1"})
        if request.headers.get("APCA-API-KEY-ID") != "test-key":
            return httpx.Response(401, json={"message": "unauthorized"})

        path = request.url.path
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if path == "/v2/account":
            return httpx.Response(200, json={"status": "ACTIVE", "currency": "USD", "equity": "100000", "cash": "100000",
                                             "buying_power": "200000", "pattern_day_trader": False})
        if path == "/v2/clock":
            return httpx.Response(200, json={"is_open": self.is_open})
        if path == "/v2/calendar":
            return httpx.Response(200, json=[c for c in self.calendar if q["start"] <= c["date"] <= q["end"]])
        if path == "/v2/stocks/bars":
            return self._bars(q)
        return httpx.Response(404, json={"message": "not found"})

    def _bars(self, q: dict[str, str]) -> httpx.Response:
        start, end = q["start"], q["end"]
        flat = [
            (sym, row)
            for sym in q["symbols"].split(",")
            for row in self.bars.get(sym, [])
            if start <= row["t"] < end
        ]
        offset = int(q.get("page_token", 0))
        page = flat[offset : offset + self.page_size]
        out: dict[str, list[dict]] = {}
        for sym, row in page:
            out.setdefault(sym, []).append(row)
        nxt = offset + self.page_size
        return httpx.Response(200, content=json.dumps({"bars": out, "next_page_token": str(nxt) if nxt < len(flat) else None}))


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)
