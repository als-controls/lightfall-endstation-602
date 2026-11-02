from lightfall_endstation_602.contract import CONTRACT_VERSION, build_rixs_md


def test_build_rixs_md_shape():
    assert build_rixs_md("marana_image", "sm3_pitch", (2048, 2048), 850.0) == {
        "contract_version": CONTRACT_VERSION,
        "kind": "zeroth_order_focus",
        "image_field": "marana_image",
        "motor_field": "sm3_pitch",
        "frame_shape": [2048, 2048],
        "mono_energy_ev": 850.0,
    }
