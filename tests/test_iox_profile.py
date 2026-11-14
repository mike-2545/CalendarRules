"""Offline checks for the single-node IoX profile shipped with CalendarRules."""

import ast
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
PROFILE = PLUGIN / "profile"


def test_profile_has_the_expected_three_assets_and_version():
    for name in (
        "nodedef/nodedefs.xml",
        "editor/editors.xml",
        "nls/en_us.txt",
        "version.txt",
    ):
        assert (PROFILE / name).is_file(), name
    assert (PROFILE / "version.txt").read_text().strip() == "1.2.0"


def test_nodedef_is_controller_only_with_native_status_drivers():
    root = ET.parse(PROFILE / "nodedef" / "nodedefs.xml").getroot()
    assert root.tag == "nodeDefs"
    nodes = root.findall("nodeDef")
    assert len(nodes) == 1
    assert nodes[0].attrib == {"id": "calctrl", "nls": "calctrl"}
    statuses = nodes[0].findall("./sts/st")
    assert [status.attrib for status in statuses] == [
        {"id": "ST", "editor": "online"},
        {"id": "GV0", "editor": "refreshStatus"},
        {"id": "GV1", "editor": "cacheStatus"},
        {"id": "GV2", "editor": "statusCount"},
        {"id": "GV3", "editor": "statusCount"},
        {"id": "GV4", "editor": "ioxStatus"},
    ]
    assert nodes[0].find("cmds/sends") is not None
    assert nodes[0].find("cmds/accepts") is not None
    assert list(nodes[0].find("cmds/sends")) == []
    accepts = list(nodes[0].find("cmds/accepts"))
    assert len(accepts) == 1
    assert accepts[0].tag == "cmd"
    assert accepts[0].attrib == {"id": "REFRESH"}


def test_status_editors_match_driver_uoms_ranges_and_labels():
    editors = ET.parse(PROFILE / "editor" / "editors.xml").getroot()
    assert editors.tag == "editors"
    entries = editors.findall("editor")
    assert [entry.attrib for entry in entries] == [
        {"id": "online"},
        {"id": "refreshStatus"},
        {"id": "cacheStatus"},
        {"id": "statusCount"},
        {"id": "ioxStatus"},
    ]
    assert entries[0].find("range").attrib == {
        "uom": "25",
        "subset": "0,1",
        "nls": "ONLINE",
    }
    assert entries[1].find("range").attrib == {
        "uom": "25",
        "subset": "0,1,2,3,4",
        "nls": "REFRESH_STATUS",
    }
    assert entries[2].find("range").attrib == {
        "uom": "25",
        "subset": "0,1",
        "nls": "CACHE_STATUS",
    }
    assert entries[3].find("range").attrib == {
        "uom": "56",
        "min": "0",
        "max": "10000",
        "prec": "0",
        "step": "1",
    }
    assert entries[4].find("range").attrib == {
        "uom": "25",
        "subset": "0,1,2,3",
        "nls": "IOX_STATUS",
    }
    labels = (PROFILE / "nls" / "en_us.txt").read_text()
    for label in (
        "ND-calctrl-NAME = Google Calendar",
        "ST-calctrl-ST-NAME = Online",
        "ST-calctrl-GV0-NAME = Refresh Status",
        "ST-calctrl-GV1-NAME = Calendar Cache",
        "ST-calctrl-GV2-NAME = Events Evaluated",
        "ST-calctrl-GV3-NAME = Targets Desired ON",
        "ST-calctrl-GV4-NAME = IoX Communication",
        "ONLINE-0 = Disconnected",
        "ONLINE-1 = Connected",
        "REFRESH_STATUS-0 = Idle",
        "REFRESH_STATUS-1 = In Progress",
        "REFRESH_STATUS-2 = Succeeded",
        "REFRESH_STATUS-3 = Failed / Using Cache",
        "REFRESH_STATUS-4 = Failed",
        "CACHE_STATUS-0 = Unavailable / Untrusted",
        "CACHE_STATUS-1 = Trusted",
        "IOX_STATUS-0 = Authorized / Readable",
        "IOX_STATUS-1 = Unauthorized",
        "IOX_STATUS-2 = Failed",
        "IOX_STATUS-3 = Pending",
        "CMD-calctrl-REFRESH-NAME = Refresh Now",
    ):
        assert label in labels


def test_launcher_registers_controller_after_ready_once():
    main = ast.parse((PLUGIN / "main.py").read_text())
    func = next(node for node in main.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    calls = [
        node.value.func.attr
        for node in func.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
    ]
    assert calls == ["start", "ready", "register_node", "runForever"]

    profile_requests = [
        node
        for node in func.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_publish_static_profile"
    ]
    assert len(profile_requests) == 1
    assert func.body.index(profile_requests[0]) > next(
        index
        for index, node in enumerate(func.body)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "ready"
    )
    assert func.body.index(profile_requests[0]) < next(
        index
        for index, node in enumerate(func.body)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "register_node"
    )


def test_profile_contains_only_the_read_only_refresh_action():
    all_profile_text = "\n".join(
        (PROFILE / path).read_text()
        for path in ("nodedef/nodedefs.xml", "editor/editors.xml", "nls/en_us.txt")
    ).lower()
    assert all_profile_text.count('<cmd id="refresh"') == 1
    for forbidden in ("oauth", "googleapiclient", "set_variable", "setvar", "<p id="):
        assert forbidden not in all_profile_text
