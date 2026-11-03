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


def test_subjects_match_plugin():
    names = [n for n in dir(contract) if n.startswith("SUBJ_")] + ["MD_KEY", "CONTRACT_VERSION"]
    assert len([n for n in names if n.startswith("SUBJ_")]) == 8
    assert contract.CONTRACT_VERSION == 2
    for n in names:
        assert getattr(contract, n) == getattr(plugin_contract, n), n
