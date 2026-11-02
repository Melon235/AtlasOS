from csv_parser import parse_csv


def test_plain_rows() -> None:
    assert parse_csv("name,city\nAlice,Tokyo\n") == [
        ["name", "city"],
        ["Alice", "Tokyo"],
    ]


def test_blank_lines_are_ignored() -> None:
    assert parse_csv("\nname,age\n\nBob,42\n\n") == [
        ["name", "age"],
        ["Bob", "42"],
    ]
