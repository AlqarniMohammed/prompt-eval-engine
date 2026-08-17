from src.utils.bundle import format_bundle, parse_bundle, to_list


def test_roundtrip():
    bundle = format_bundle("hello world", {"a/b.md": "content one", "c.py": "print(1)"})
    parsed = parse_bundle(bundle)
    assert parsed["stdout"] == "hello world"
    assert parsed["files"] == {"a/b.md": "content one", "c.py": "print(1)"}


def test_parse_tolerates_missing_stdout_header():
    parsed = parse_bundle("just raw text, no headers")
    assert parsed["stdout"] == "" and parsed["files"] == {}


def test_to_list_accepts_csv_and_arrays():
    assert to_list("a, b ,c") == ["a", "b", "c"]
    assert to_list(["x", 1]) == ["x", "1"]
    assert to_list(None) == []
