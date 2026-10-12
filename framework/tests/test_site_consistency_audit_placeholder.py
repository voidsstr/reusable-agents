from framework.cli.site_consistency_audit import PLACEHOLDER


def test_nan_is_case_sensitive():
    assert PLACEHOLDER.search("Price: NaN")
    assert not PLACEHOLDER.search("Nan Gyi Thoke Thick Rice Noodle Salad")
    assert not PLACEHOLDER.search("garlic nan bread")


def test_other_placeholders_still_case_insensitive():
    assert PLACEHOLDER.search("Lorem Ipsum dolor")
    assert PLACEHOLDER.search("todo: fill in")
