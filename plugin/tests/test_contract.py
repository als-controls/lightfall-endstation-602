from lightfall_endstation_602.contract import CONTRACT_VERSION, build_rixs_md


def test_build_rixs_md_shape():
    assert build_rixs_md("marana_image", "sm3_pitch", (2048, 2048), 850.0) == {
        "contract_version": CONTRACT_VERSION,
        "kind": "zeroth_order_focus",
        "image_field": "marana_image",
        "motor_field": "sm3_pitch",
        "frame_shape": [2048, 2048],
        "mono_energy_ev": 850.0,
        "motor_units": "",
    }


def test_build_rixs_md_motor_units():
    md = build_rixs_md("marana_image", "sample_x", (2048, 2048), 850.0, motor_units="mm")
    assert md["motor_field"] == "sample_x" and md["motor_units"] == "mm"


def test_build_rixs_md_nd_fields_only_when_given():
    md = build_rixs_md("marana_image", "sm3_pitch", (2048, 2048), None)
    assert "image_stream" not in md and "motor_fields" not in md
    md = build_rixs_md("marana_image", "sm3_pitch", (2048, 2048), None,
                       image_stream="frames", motor_fields=["sm3_pitch", "sample_x"])
    assert md["image_stream"] == "frames"
    assert md["motor_fields"] == ["sm3_pitch", "sample_x"]
    assert md["motor_field"] == "sm3_pitch"


def test_build_rixs_md_motor_fields_must_start_with_motor_field():
    import pytest
    with pytest.raises(ValueError):
        build_rixs_md("marana_image", "sm3_pitch", (8, 8), None, motor_fields=["sample_x"])
