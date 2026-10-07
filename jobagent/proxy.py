"""DataImpulse proxy configuration, a shared HTTP client, and a traffic/cost meter."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

LEDGER = Path(__file__).resolve().parent.parent / "usage.log"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


@dataclass
class Meter:
    """Approximate proxy traffic: response bytes on the wire plus request/response headers.

    DataImpulse bills its own measurement (see the dashboard's Usage Chart); this is an
    estimate for showing cost as you go.
    """

    requests: int = 0
    bytes: int = 0
    latencies: list[float] = field(default_factory=list)
    _logged: int = 0

    @classmethod
    def from_ledger(cls) -> "Meter":
        """Cumulative usage across all runs since the ledger was last reset."""
        m = cls()
        if LEDGER.exists():
            for line in LEDGER.read_text().split("\n"):
                if line:
                    b, t = line.split()
                    m.requests, m.bytes = m.requests + 1, m.bytes + int(b)
                    m.latencies.append(float(t))
        return m

    def record(self, r: httpx.Response) -> None:
        headers = sum(len(k) + len(v) + 4 for k, v in r.headers.raw) + \
                  sum(len(k) + len(v) + 4 for k, v in r.request.headers.raw)
        self.requests += 1
        body_out = int(r.request.headers.get("content-length", 0) or 0)  # never touch a streamed body
        self.bytes += r.num_bytes_downloaded + headers + body_out
        self.latencies.append(r.elapsed.total_seconds())
        if os.getenv("USE_PROXY", "1") != "0":
            with open(LEDGER, "a") as f:
                f.write(f"{self.bytes - self._logged} {r.elapsed.total_seconds():.3f}\n")
            self._logged = self.bytes

    @property
    def mb(self) -> float:
        return self.bytes / 1_000_000

    @property
    def size(self) -> str:
        return f"{self.mb:.2f} MB" if self.bytes >= 100_000 else f"{self.bytes / 1000:.1f} KB"

    @property
    def cost(self) -> float:
        return self.bytes / 1_000_000_000 * float(os.getenv("DATAIMPULSE_PRICE_PER_GB", "1"))

    def receipt(self) -> str:
        if not self.requests:
            return "No requests."
        lat = sorted(self.latencies)
        median = lat[len(lat) // 2]
        note = " (proxy OFF — nothing billed)" if os.getenv("USE_PROXY", "1") == "0" else ""
        return (f"{self.requests} requests · {self.size} · median response {median:.2f}s · "
                f"est. cost ${self.cost:.{4 if self.cost >= 0.0001 else 6}f} at ${os.getenv('DATAIMPULSE_PRICE_PER_GB', '1')}/GB{note}")


meter = Meter()


class MeteredClient(httpx.AsyncClient):
    async def send(self, request, *args, **kwargs):
        r = await super().send(request, *args, **kwargs)
        if not kwargs.get("stream"):
            meter.record(r)
        return r


def proxy_url(country: str | None = None, sticky: bool = False) -> str | None:
    """Build the DataImpulse proxy URL, or None when the proxy is disabled.

    sticky=True uses DATAIMPULSE_STICKY_PORT (same exit IP for the session duration you
    set in the dashboard); otherwise DATAIMPULSE_PORT (rotating, new IP per request).
    """
    if os.getenv("USE_PROXY", "1") == "0":
        return None
    login = os.getenv("DATAIMPULSE_LOGIN")
    password = os.getenv("DATAIMPULSE_PASSWORD")
    if not login or not password:
        raise RuntimeError(
            "DATAIMPULSE_LOGIN / DATAIMPULSE_PASSWORD are not set. Copy .env.example "
            "to .env and fill them in, or set USE_PROXY=0."
        )
    host = os.getenv("DATAIMPULSE_HOST", "gw.dataimpulse.com")
    port = os.getenv("DATAIMPULSE_STICKY_PORT", "10000") if sticky else os.getenv("DATAIMPULSE_PORT", "823")
    scheme = "socks5" if os.getenv("DATAIMPULSE_PROTOCOL", "http").lower() == "socks5" else "http"
    country = country or os.getenv("DATAIMPULSE_COUNTRY")
    user = f"{login}__cr.{country.lower()}" if country else login
    return f"{scheme}://{user}:{password}@{host}:{port}"


def client(country: str | None = None, timeout: float = 30.0, sticky: bool = False,
           new_ip_per_request: bool = False) -> httpx.AsyncClient:
    """new_ip_per_request disables keep-alive so each request opens a fresh connection,
    which the rotating gateway serves from a different residential IP."""
    proxy = proxy_url(country, sticky)
    return MeteredClient(
        proxy=proxy,
        timeout=timeout,
        limits=httpx.Limits(max_keepalive_connections=0) if new_ip_per_request else httpx.Limits(),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )
