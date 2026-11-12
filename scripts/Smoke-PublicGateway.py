"""Probe the existing HTTPS gateway without reading cases or printing secrets."""
from __future__ import annotations

import http.client
import socket
import ssl
from pathlib import Path
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip().strip("\"'")
    return values


class PinnedHttps(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str):
        super().__init__(host, timeout=15, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        raw = socket.create_connection((self.ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def main() -> None:
    public = read_env(ROOT / ".env")
    infra = read_env(Path(public["WINDCATCHER_INFRA_ENV"]))
    parsed = urlsplit(public["WINDCATCHER_PUBLIC_URL"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
        raise RuntimeError("Public URL must be an HTTPS origin")
    host = parsed.hostname
    ip = infra["ALIBABA_SERVER_IP"]
    def probe(path: str, *, headers: dict | None = None, method: str = "GET", body: bytes | None = None):
        connection = PinnedHttps(host, ip)
        try:
            connection.request(method, path, body=body, headers={"Host": host, **(headers or {})})
            response = connection.getresponse()
            return response.status, response.getheader("Set-Cookie"), response.read()
        finally:
            connection.close()

    invalid_token = "A" * 43
    checks = {
        "public_login_page": (probe("/")[0], 200),
        "login_route": (probe("/login")[0], 200),
        "health": (probe("/healthz")[0], 200),
        "login_brand_asset": (probe("/static/assets/logo-badge.svg")[0], 200),
        "patient_invalid_link": (probe("/share/" + invalid_token)[0], 404),
        "patient_invalid_model": (probe("/share/" + invalid_token + "/model")[0], 404),
        "patient_asset": (probe("/static/patient-view.js")[0], 200),
        "anonymous_research_api": (probe("/api/cases")[0], 401),
        "anonymous_activity_log": (probe("/api/monitor/status")[0], 401),
        "anonymous_cfd_review": (probe("/api/cases/case-19000101-000000-dead/cfd-reviews")[0], 401),
        "anonymous_formal_ports": (probe("/api/cases/case-19000101-000000-dead/segment/ports/formal")[0], 401),
        "anonymous_three_port_proposal": (probe(
            "/api/cases/case-19000101-000000-dead/segment/endplanes/learning/auto-propose-bilateral",
            headers={"Origin": parsed.scheme + "://" + host, "Content-Type": "application/json"},
            method="POST", body=b'{"scope":"main_airway","mask_version":1}')[0], 401),
        "anonymous_single_port_suggestion": (probe(
            "/api/cases/case-19000101-000000-dead/segment/endplanes/suggest-single-port",
            headers={"Origin": parsed.scheme + "://" + host, "Content-Type": "application/json"},
            method="POST", body=b'{"scope":"main_airway","mask_version":1,"side":"left","format":"oblique_bilateral_local_v3"}')[0], 401),
        "anonymous_harness_advice": (probe("/api/cases/case-19000101-000000-dead/harness-advice",
                                           headers={"Origin": parsed.scheme + "://" + host},
                                           method="POST")[0], 401),
        "anonymous_admin": (probe("/share-admin")[0], 401),
    }
    login_headers = {"Origin": parsed.scheme + "://" + host,
                     "Content-Type": "application/x-www-form-urlencoded"}
    if ((public["WINDCATCHER_CLOUD_USERNAME"], public["WINDCATCHER_CLOUD_PASSWORD"]) !=
            (public["WINDCATCHER_PUBLIC_USERNAME"], public["WINDCATCHER_PUBLIC_PASSWORD"])):
        cloud_form = urlencode({"username": public["WINDCATCHER_CLOUD_USERNAME"],
                                "password": public["WINDCATCHER_CLOUD_PASSWORD"]}).encode()
        cloud_status, cloud_cookie, _ = probe("/login", headers=login_headers, method="POST", body=cloud_form)
        checks["cloud_credentials_rejected"] = (cloud_status, 303)
        if cloud_cookie:
            raise RuntimeError("ModelScope credentials unexpectedly issued a public session")
    form = urlencode({"username": public["WINDCATCHER_PUBLIC_USERNAME"],
                      "password": public["WINDCATCHER_PUBLIC_PASSWORD"]}).encode()
    login_status, set_cookie, _ = probe("/login", headers=login_headers,
                                        method="POST", body=form)
    checks["research_login"] = (login_status, 303)
    if not set_cookie or "__Host-windcatcher=" not in set_cookie:
        raise RuntimeError("Research login did not issue a session cookie")
    cookie = set_cookie.split(";", 1)[0]
    dashboard_status, _, dashboard = probe("/", headers={"Cookie": cookie})
    checks["research_dashboard"] = (dashboard_status, 200)
    if (b'data-route="activity"' not in dashboard
            or b'window.WindcatcherActivityAccess = true' not in dashboard):
        raise RuntimeError("Research dashboard did not expose the authenticated activity page")
    if public.get("WINDCATCHER_HARNESS_ADVICE") == "1":
        if b'window.WindcatcherHarnessEnabled = true;' not in dashboard:
            raise RuntimeError("Research dashboard did not enable Harness advice")
        checks["harness_advice_asset"] = (probe("/static/harness-advice.js",
                                                headers={"Cookie": cookie})[0], 200)
        checks["unknown_harness_case"] = (probe(
            "/api/cases/case-19000101-000000-dead/harness-advice",
            headers={"Cookie": cookie, "Origin": parsed.scheme + "://" + host},
            method="POST")[0], 404)
    checks["authenticated_activity_log"] = (probe("/api/monitor/status", headers={"Cookie": cookie})[0], 200)
    checks["authenticated_activity_events"] = (probe("/api/monitor/events", headers={"Cookie": cookie})[0], 200)
    checks["authenticated_activity_asset"] = (probe("/static/activity-monitor.js", headers={"Cookie": cookie})[0], 200)
    checks["cfd_review_asset"] = (probe("/static/cfd-review-admin.js", headers={"Cookie": cookie})[0], 200)
    checks["unknown_cfd_review_case"] = (probe("/api/cases/case-19000101-000000-dead/cfd-reviews",
                                               headers={"Cookie": cookie})[0], 404)
    checks["unknown_formal_ports_case"] = (probe(
        "/api/cases/case-19000101-000000-dead/segment/ports/formal",
        headers={"Cookie": cookie})[0], 404)
    checks["unknown_three_port_proposal_case"] = (probe(
        "/api/cases/case-19000101-000000-dead/segment/endplanes/learning/auto-propose-bilateral",
        headers={"Cookie": cookie, "Origin": parsed.scheme + "://" + host,
                 "Content-Type": "application/json"}, method="POST",
        body=b'{"scope":"main_airway","mask_version":1}')[0], 404)
    checks["unknown_single_port_suggestion_case"] = (probe(
        "/api/cases/case-19000101-000000-dead/segment/endplanes/suggest-single-port",
        headers={"Cookie": cookie, "Origin": parsed.scheme + "://" + host,
                 "Content-Type": "application/json"}, method="POST",
        body=b'{"scope":"main_airway","mask_version":1,"side":"left","format":"oblique_bilateral_local_v3"}')[0], 404)
    checks["legacy_admin_redirect"] = (probe("/share-admin", headers={"Cookie": cookie})[0], 303)
    checks["local_tools_blocked"] = (probe("/api/pick-directory", headers={"Cookie": cookie})[0], 403)
    checks["cross_origin_blocked"] = (probe("/logout", headers={"Cookie": cookie,
                                                                 "Origin": "https://untrusted.invalid"}, method="POST")[0], 403)
    for name, (actual, expected) in checks.items():
        print(f"{name}: HTTP {actual}")
        if actual != expected:
            raise RuntimeError(f"{name}: expected HTTP {expected}, received {actual}")
    print("HTTPS certificate and host verified; no case data requested.")


if __name__ == "__main__":
    main()
