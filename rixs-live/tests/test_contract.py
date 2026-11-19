import pytest

from lightfall_endstation_602 import contract as plugin_contract
from rixs_live import contract
from rixs_live.contract import RixsRunInfo, parse_start_rixs


def _doc(**over):
    md = {
        "contract_version": contract.CONTRACT_VERSION,
        "kind": "zeroth_order_focus",
        "image_field": "marana_image",
        "motor_field": "sm3_pitch",
        "frame_shape": [2048, 1024],
        "mono_energy_ev": 850.0,
    }
    md.update(over)
    return {"rixs": md}


def test_parse_ok():
    assert parse_start_rixs("u1", _doc()) == RixsRunInfo(
        "u1", "marana_image", "sm3_pitch", (2048, 1024), 850.0
    )


def test_parse_motor_units_optional():
    assert parse_start_rixs("u1", _doc()).motor_units == ""  # older senders omit it
    info = parse_start_rixs("u1", _doc(motor_field="sample_x", motor_units="mm"))
    assert (info.motor_field, info.motor_units) == ("sample_x", "mm")
    assert parse_start_rixs("u1", _doc(motor_units=None)).motor_units == ""


def test_plugin_md_round_trips():
    md = plugin_contract.build_rixs_md("marana_image", "sample_x", (2048, 2048), 850.0,
                                       motor_units="mm")
    assert parse_start_rixs("u1", {"rixs": md}) == RixsRunInfo(
        "u1", "marana_image", "sample_x", (2048, 2048), 850.0, "mm")


def test_parse_absent_returns_none():
    assert parse_start_rixs("u1", {"plan_name": "scan"}) is None


def test_parse_bad_version_raises():
    with pytest.raises(ValueError):
        parse_start_rixs("u1", _doc(contract_version=99))
    with pytest.raises(ValueError):
        parse_start_rixs("u1", _doc(kind="other"))
    bad = _doc()
    del bad["rixs"]["frame_shape"]
    with pytest.raises(ValueError):
        parse_start_rixs("u1", bad)
    with pytest.raises(ValueError):
        parse_start_rixs("u1", _doc(frame_shape=[5]))


def test_service_names_match_plugin():
    prefixes = ("RPC_", "EVT_", "SERVICE_")
    names = [n for n in dir(contract) if n.startswith(prefixes)] + ["MD_KEY", "CONTRACT_VERSION"]
    assert len([n for n in names if n.startswith(prefixes)]) == 9
    assert contract.CONTRACT_VERSION == 2
    assert not [n for n in dir(contract) if n.startswith("SUBJ_")]  # no public subjects
    for n in names:
        assert getattr(contract, n) == getattr(plugin_contract, n), n


def test_parse_image_stream_and_motor_fields_optional():
    info = parse_start_rixs("u1", _doc())
    assert (info.image_stream, info.motor_fields) == ("primary", ())
    assert info.fields == ("sm3_pitch",) and not info.multi_motor
    info = parse_start_rixs("u1", _doc(image_stream="frames",
                                       motor_fields=["sm3_pitch", "sample_x", "aux"]))
    assert info.image_stream == "frames"
    assert info.fields == ("sm3_pitch", "sample_x", "aux") and info.multi_motor
    one = parse_start_rixs("u1", _doc(motor_fields=["sm3_pitch"]))
    assert one.fields == ("sm3_pitch",) and not one.multi_motor


@pytest.mark.parametrize("over", [
    {"image_stream": ""}, {"image_stream": 5}, {"motor_fields": []},
    {"motor_fields": "sm3_pitch"}, {"motor_fields": ["sample_x", "sm3_pitch"]},
    {"motor_fields": ["sm3_pitch", "sm3_pitch"]}, {"motor_fields": ["sm3_pitch", 3]},
])
def test_parse_bad_nd_fields_raise(over):
    with pytest.raises(ValueError):
        parse_start_rixs("u1", _doc(**over))


def test_plugin_nd_md_round_trips():
    md = plugin_contract.build_rixs_md(
        "marana_image", "sm3_pitch", (2048, 2048), None, image_stream="frames",
        motor_fields=["sm3_pitch", "sample_x"])
    assert md["image_stream"] == "frames" and md["motor_fields"] == ["sm3_pitch", "sample_x"]
    info = parse_start_rixs("u1", {"rixs": md})
    assert info == RixsRunInfo("u1", "marana_image", "sm3_pitch", (2048, 2048), None, "",
                               "frames", ("sm3_pitch", "sample_x"))
    with pytest.raises(ValueError):
        plugin_contract.build_rixs_md("marana_image", "sm3_pitch", (8, 8), None,
                                      motor_fields=["sample_x"])


def test_plugin_pva_md_round_trips():
    md = plugin_contract.build_rixs_md(
        "marana_image", "sm3_pitch", (2048, 2048), 850.0,
        pva_channel="BL602:marana01:Pva1:Image", frame_counter_field="marana_array_counter")
    info = parse_start_rixs("u1", {"rixs": md})
    assert info.uses_pva
    assert info.pva_channel == "BL602:marana01:Pva1:Image"
    assert info.frame_counter_field == "marana_array_counter"
    assert not parse_start_rixs("u2", _doc()).uses_pva


@pytest.mark.parametrize("over", [{"pva_channel": "X:Image"},
                                  {"frame_counter_field": "c"},
                                  {"pva_channel": "", "frame_counter_field": "c"},
                                  {"pva_channel": 3, "frame_counter_field": "c"}])
def test_parse_pva_fields_go_together(over):
    with pytest.raises(ValueError, match="pva_channel"):
        parse_start_rixs("u1", _doc(**over))
