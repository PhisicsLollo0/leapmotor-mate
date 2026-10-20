"""MATE_READ_ONLY: an install that only watches the car. Every command leaves through one door,
`_wire`, so the door is shut there — the web, MQTT and the poller's own automations alike — and the
page and Home Assistant stop offering what could not be sent."""
import pytest

import mate_api  # noqa: F401 — puts poller/mate_api_runtime on sys.path, as the poller process does
import api_v2_bridge as bridge
import ui_command_access
from leapmotor_cloud.cloud import CENTER_ORIGIN
from cloud_access_fixture import settings

VIN = 'LMRTESTREADONLY01'
COMMAND_DOORS = [bridge.CONTROL_PATH, bridge.VERIFY_PATH, bridge.RESULT_PATH, bridge.APPOINTMENT_PATH]


@pytest.fixture
def read_only(monkeypatch):
    monkeypatch.setenv('MATE_READ_ONLY', '1')


@pytest.mark.parametrize('path', COMMAND_DOORS)
def test_no_command_leaves_a_read_only_install(read_only, path):
    api = object.__new__(bridge.NewAPIClient)
    with pytest.raises(bridge.LeapmotorApiError, match='Command not sent: read_only'):
        api._wire(CENTER_ORIGIN, path, {'vin': VIN})


def test_reads_still_leave(read_only):
    api = object.__new__(bridge.NewAPIClient)      # no session at all: a read gets past the door
    with pytest.raises(Exception) as caught:       # and only then fails, on what it lacks
        api._wire(CENTER_ORIGIN, bridge.GET_APPOINTMENT_PATH, {'vin': VIN})
    assert 'read_only' not in str(caught.value)


def test_the_page_and_home_assistant_offer_no_command(read_only):
    get = settings(VIN)
    assert not ui_command_access.command_allowed(VIN, 'lock', get)
    assert 'api/command/lock' in ui_command_access.hidden_controls_css(VIN, get)


def test_without_the_switch_nothing_changes(monkeypatch):
    monkeypatch.delenv('MATE_READ_ONLY', raising=False)
    assert ui_command_access.command_allowed(VIN, 'lock', settings(VIN))


@pytest.mark.parametrize('value, on', [('1', True), ('true', True), ('YES', True), (' on ', True),
                                       ('', False), ('0', False), ('false', False), ('no', False)])
def test_the_switch_reads_as_written(monkeypatch, value, on):
    from read_only import read_only as is_on
    monkeypatch.setenv('MATE_READ_ONLY', value)
    assert is_on() is on


def test_the_command_endpoint_says_why(read_only):
    pytest.importorskip("fastapi", reason="web.main needs fastapi")
    from starlette.testclient import TestClient
    import main
    r = TestClient(main.app).post('/api/command/lock')
    assert r.status_code == 403
    assert 'MATE_READ_ONLY' in r.text
