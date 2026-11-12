"""The reverse proxy must delegate activity authorization to the app."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_gateway_does_not_block_authenticated_activity_routes():
    caddy = (ROOT / 'deploy' / 'public-gateway.caddy').read_text(encoding='utf-8')
    tools_rule = next(line for line in caddy.splitlines() if '@local_tools path' in line)
    assert '/api/monitor/*' not in tools_rule
    assert 'reverse_proxy 127.0.0.1:18765' in caddy


def test_public_smoke_checks_activity_authentication_and_logged_in_access():
    smoke = (ROOT / 'scripts' / 'Smoke-PublicGateway.py').read_text(encoding='utf-8')
    assert 'anonymous_activity_log' in smoke
    assert 'authenticated_activity_log' in smoke
