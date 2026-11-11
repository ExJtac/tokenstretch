from tokenstretch.mask import mask_finding_evidence, mask_value


def test_mask_secretish_keys_and_values():
    ev = mask_finding_evidence({
        "api_key": "sk-ant-fixture-abcdefghijk",
        "file": "/tmp/settings.json",
        "value": "Bearer eyJaaaaaaaaaaaaaaaaaaaa",
    })
    assert ev["api_key"] == "[redacted]"
    assert ev["file"] == "/tmp/settings.json"
    assert ev["value"] == "[redacted]"
    assert mask_value("token", "visible") == "[redacted]"
