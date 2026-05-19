import re

def test_code_version_is_short_string():
    from model import CODE_VERSION
    assert isinstance(CODE_VERSION, str)
    assert 4 <= len(CODE_VERSION) <= 40
    assert re.fullmatch(r"[0-9a-f]+", CODE_VERSION)

def test_code_version_stable_across_imports():
    from model import CODE_VERSION as v1
    from model import CODE_VERSION as v2
    assert v1 == v2
