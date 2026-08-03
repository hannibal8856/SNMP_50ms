import textwrap
import pytest
from walkdiff import parse_walk, diff, load_profile


def write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(textwrap.dedent(content).lstrip("\n"))
    return str(p)


def test_parse_walk_extracts_oid_and_value(tmp_path):
    f = write(tmp_path, "w.txt", """
        iso.3.6.1.2.1.1.1.0 = ""
        iso.3.6.1.2.1.2.2.1.1.1 = INTEGER: 1
        End of MIB
    """)
    oids, complete = parse_walk(f)
    assert oids == {
        "1.3.6.1.2.1.1.1.0": '""',
        "1.3.6.1.2.1.2.2.1.1.1": "INTEGER: 1",
    }
    assert complete is True


def test_parse_walk_flags_truncated_capture(tmp_path):
    f = write(tmp_path, "w.txt", """
        iso.3.6.1.2.1.1.1.0 = ""
        iso.3.6.1.2.1.2.2.1.1.1 = INTEGER: 1
    """)
    _, complete = parse_walk(f)
    assert complete is False


def test_diff_reports_all_three_categories():
    a = {"1.1": "INTEGER: 1", "1.2": "INTEGER: 2", "1.3": "INTEGER: 3"}
    b = {"1.2": "INTEGER: 99", "1.3": "INTEGER: 3", "1.4": "INTEGER: 4"}
    only_a, only_b, differs = diff(a, b)
    assert only_a == {"1.1"}
    assert only_b == {"1.4"}
    assert differs == [("1.2", "INTEGER: 2", "INTEGER: 99")]


def test_load_profile_splits_private_and_standard(tmp_path):
    f = write(tmp_path, "m.profile", """
        ### Private MIBs
        mxPort=YES
        mxLa=YES
        ### Standard MIBs
        IF-MIB=YES
        BRIDGE-MIB=YES
    """)
    private, standard = load_profile(f)
    assert private == {"mxPort", "mxLa"}
    assert standard == {"IF-MIB", "BRIDGE-MIB"}
