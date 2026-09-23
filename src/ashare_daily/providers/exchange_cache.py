"""Same-day, content-verified cache of public SSE security-type responses.

List pages always reach the source so each run verifies its own end boundary.
This cache is not a historical list or an independent reconciliation source.
"""
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode

from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.operations.daily import atomic_json
from ashare_daily.providers.exchange_universe import SSE_METADATA_ENDPOINT, WebsiteTransport


class CachedWebsiteTransport:
    def __init__(self, directory: Path, *, cache_root: Path, timeout_seconds=20,
                 environment_label="local_windows_cli"):
        self.directory = Path(directory)
        self.cache_root = Path(cache_root)
        self.fetch = WebsiteTransport(directory, timeout_seconds=timeout_seconds,
                                      environment_label=environment_label)

    def __call__(self, endpoint: str, params: dict, *, label: str, target: date) -> dict:
        eligible = (endpoint == SSE_METADATA_ENDPOINT and
                    params.get("sqlId") == "COMMON_SSE_CP_GPJCTPZ_GPLB_GPGK_GSGK_C" and
                    target == datetime.now(SHANGHAI).date())
        key = hashlib.sha256(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()
        packet_path = self.cache_root / target.isoformat() / (key + ".json")
        if eligible:
            try:
                packet = json.loads(packet_path.read_text(encoding="utf-8"))
                response = packet["response"]
                raw = bytes.fromhex(packet["raw_hex"])
                observed = datetime.fromisoformat(response["fetched_at"])
                digest = hashlib.sha256(raw).hexdigest()
                valid = (packet.get("schema_version") == "f11-exchange-type-cache-v1" and
                         packet.get("provenance_mode") == "online" and response.get("ok") is True and
                         response.get("verification_kind") == "live_network" and response.get("provenance_mode") == "online" and
                         response.get("http_status") == 200 and response.get("params") == params and
                         response.get("url") == endpoint + "?" + urlencode(params) and
                         response.get("target_date") == target.isoformat() and observed.tzinfo is not None and
                         observed.astimezone(SHANGHAI).date() == target and observed <= datetime.now(SHANGHAI) and
                         digest == response.get("raw_sha256") and json.loads(raw.decode("utf-8-sig")) == response["body"])
                if valid:
                    # Retain original observation time and raw bytes in this run.
                    destination = self.directory / ("cached-" + key)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.with_suffix(".raw").write_bytes(raw)
                    response = dict(response, cached=True, cache_packet=str(packet_path.resolve()),
                                    cache_scope="scope_neutral_security_metadata", cached_conclusions=False,
                                    raw_path=str(destination.with_suffix(".raw").resolve()),
                                    response_path=str(destination.with_suffix(".json").resolve()),
                                    reused_at=datetime.now(SHANGHAI).isoformat())
                    atomic_json(destination.with_suffix(".json"), response)
                    return response
            except (OSError, KeyError, TypeError, ValueError, UnicodeError):
                pass  # Missing, stale, malformed or tampered evidence is a cache miss.
        response = self.fetch(endpoint, params, label=label, target=target)
        if eligible and response.get("ok") is True and response.get("verification_kind") == "live_network":
            raw = Path(response["raw_path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() == response.get("raw_sha256"):
                atomic_json(packet_path, {"schema_version": "f11-exchange-type-cache-v1",
                            "provenance_mode": "online", "cache_scope": "scope_neutral_security_metadata",
                            "cached_conclusions": False, "response": response, "raw_hex": raw.hex()})
        return response
