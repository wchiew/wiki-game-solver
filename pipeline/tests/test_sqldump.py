"""Tests for the MySQL dump tokenizer.

Two of these carry most of the weight. The **block-size sweep** exercises the
resume path that a real 7 GB dump hits constantly and that no amount of reading
the code makes obvious. The **Hypothesis round-trip** is the only thing here
that covers escape sequences we did not think to write down.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers import make_dump, mysql_quote, values
from wikigraph.sqldump import DumpFormatError, open_dump, parse_schema

PAGE_COLUMNS = (
    "page_id",
    "page_namespace",
    "page_title",
    "page_is_redirect",
    "page_is_new",
    "page_random",
    "page_touched",
    "page_links_updated",
    "page_latest",
    "page_len",
    "page_content_model",
    "page_lang",
)

#: Every row of ``fixtures/tiny/page.sql`` as
#: ``(page_id, page_namespace, page_title, page_is_redirect)``.
FIXTURE_ROWS = [
    (b"10", b"0", b"AccessibleComputing", b"1"),
    (b"12", b"0", b"Anarchism", b"0"),
    (b"13", b"0", b"Alien_(film)", b"0"),
    (b"14", b"0", b"Smith,_John", b"0"),
    (b"15", b"0", b"It's_a_Wonderful_Life", b"0"),
    (b"16", b"0", b'Say_"Hello"', b"0"),
    (b"17", b"0", b"Back\\slash", b"0"),
    (b"18", b"0", b"Trailing\\", b"0"),
    (b"19", b"0", b"Line\nBreak", b"0"),
    (b"20", b"0", b"Fifty\\%_Off", b"0"),
    (b"21", b"0", b"Doubled'Quote", b"0"),
    (b"22", b"0", "Zürich".encode(), b"0"),
    (b"23", b"0", "日本".encode(), b"0"),
    (b"24", b"1", b"Talk_target", b"0"),
    (b"25", b"10", b"Infobox", b"0"),
    (b"26", b"14", b"Films", b"0"),
    (b"27", b"0", b"Semicolon;_Test", b"0"),
    (b"28", b"0", b"Null_And_Negative", b"0"),
    (b"29", b"0", b"Hex_Touched", b"0"),
]

PAGE_PROJECTION = ("page_id", "page_namespace", "page_title", "page_is_redirect")


# --------------------------------------------------------------------------- #
# DDL
# --------------------------------------------------------------------------- #


def test_reads_table_name_and_column_order(tiny_page_dump: Path) -> None:
    schema = open_dump(tiny_page_dump).schema
    assert schema.name == "page"
    assert schema.columns == PAGE_COLUMNS


def test_an_index_named_after_a_column_is_not_a_column(tiny_page_dump: Path) -> None:
    # The fixture declares `KEY `page_random` (`page_random`)`, which a parser
    # that merely looks for backticks anywhere would count twice.
    columns = open_dump(tiny_page_dump).schema.columns
    assert columns.count("page_random") == 1


@pytest.mark.parametrize(
    "constraint",
    [
        b"  PRIMARY KEY (`a`)",
        b"  UNIQUE KEY `a_idx` (`a`)",
        b"  KEY `a` (`a`)",
        b"  FULLTEXT KEY `ft` (`b`)",
        b"  CONSTRAINT `fk` FOREIGN KEY (`a`) REFERENCES `o` (`id`)",
    ],
)
def test_constraint_lines_are_not_columns(constraint: bytes) -> None:
    ddl = b"CREATE TABLE `t` (\n  `a` int NOT NULL,\n" + constraint + b"\n) ENGINE=InnoDB;\n"
    assert parse_schema(ddl).columns == ("a",)


def test_missing_create_table_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "x.sql"
    path.write_bytes(b"-- just a comment\n")
    with pytest.raises(DumpFormatError, match="no CREATE TABLE"):
        open_dump(path)


def test_create_table_without_columns_is_an_error() -> None:
    with pytest.raises(DumpFormatError, match="no column definitions"):
        parse_schema(b"CREATE TABLE `t` (\n  PRIMARY KEY (`a`)\n);\n")


# --------------------------------------------------------------------------- #
# The fixture, and the block-size sweep
# --------------------------------------------------------------------------- #


def test_rows_match_the_fixture(tiny_page_dump: Path) -> None:
    assert list(open_dump(tiny_page_dump).rows(*PAGE_PROJECTION)) == FIXTURE_ROWS


@pytest.mark.parametrize("block_size", [1, 2, 3, 7, 13, 64, 1024, 1 << 20])
def test_block_size_does_not_change_the_result(tiny_page_dump: Path, block_size: int) -> None:
    """A tuple split across a read boundary must parse identically.

    Driving the block size down to a single byte forces the ``_NeedMore`` retry
    on essentially every token, which is the only way to be sure the resume
    path is exercised rather than merely present.
    """
    reader = open_dump(tiny_page_dump, block_size=block_size)
    assert list(reader.rows(*PAGE_PROJECTION)) == FIXTURE_ROWS


def test_rows_can_be_iterated_more_than_once(tiny_page_dump: Path) -> None:
    reader = open_dump(tiny_page_dump)
    assert list(reader.rows("page_id")) == list(reader.rows("page_id"))


def test_gzip_and_plain_read_identically(tmp_path: Path) -> None:
    body = values(b"(1,'a')", b"(2,'b')")
    plain = make_dump(tmp_path, body, name="plain.sql")
    gzipped = make_dump(tmp_path, body, name="zipped.sql", gz=True)
    assert list(open_dump(plain).rows()) == list(open_dump(gzipped).rows())


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        (rb"'plain'", b"plain"),
        (rb"''", b""),
        (rb"'a\0b'", b"a\x00b"),
        (rb"'a\'b'", b"a'b"),
        (rb"'a\"b'", b'a"b'),
        (rb"'a\bb'", b"a\x08b"),
        (rb"'a\nb'", b"a\nb"),
        (rb"'a\rb'", b"a\rb"),
        (rb"'a\tb'", b"a\tb"),
        (rb"'a\Zb'", b"a\x1ab"),
        (rb"'a\\b'", b"a\\b"),
        (rb"'end\\'", b"end\\"),
        # MySQL keeps the backslash for \% and \_ specifically.
        (rb"'50\%'", b"50\\%"),
        (rb"'a\_b'", b"a\\_b"),
        # A doubled quote is a literal quote.
        (rb"'a''b'", b"a'b"),
        (rb"''''", b"'"),
        # An unlisted escape collapses to the character itself.
        (rb"'a\qb'", b"aqb"),
        # Delimiters inside a string must not end the tuple or the statement.
        (rb"'(paren)'", b"(paren)"),
        (rb"'comma,here'", b"comma,here"),
        (rb"'semi;colon'", b"semi;colon"),
        (rb"'all),;of them'", b"all),;of them"),
    ],
)
def test_string_literals(tmp_path: Path, literal: bytes, expected: bytes) -> None:
    path = make_dump(tmp_path, values(b"(1," + literal + b")"))
    assert list(open_dump(path).rows("b")) == [(expected,)]


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        (b"NULL", None),
        (b"null", None),
        (b"42", b"42"),
        (b"-7", b"-7"),
        (b"0.5", b"0.5"),
        (b"1.5e3", b"1.5e3"),
        (b"-0.33167112649", b"-0.33167112649"),
        (b"0xAB12", b"0xAB12"),
    ],
)
def test_unquoted_literals(tmp_path: Path, literal: bytes, expected: bytes | None) -> None:
    path = make_dump(tmp_path, values(b"(1," + literal + b")"))
    assert list(open_dump(path).rows("b")) == [(expected,)]


def test_whitespace_around_values_is_ignored(tmp_path: Path) -> None:
    path = make_dump(tmp_path, b"INSERT INTO `t` VALUES\n  ( 1 , 'x' ) ,\n  ( 2 , NULL ) ;\n")
    assert list(open_dump(path).rows()) == [(b"1", b"x"), (b"2", None)]


# --------------------------------------------------------------------------- #
# Projection
# --------------------------------------------------------------------------- #


def test_projection_follows_the_requested_order(tmp_path: Path) -> None:
    path = make_dump(tmp_path, values(b"(1,'x')", b"(2,'y')"))
    assert list(open_dump(path).rows("b", "a")) == [(b"x", b"1"), (b"y", b"2")]


def test_no_projection_yields_every_column_in_ddl_order(tmp_path: Path) -> None:
    path = make_dump(tmp_path, values(b"(1,'x')"))
    assert list(open_dump(path).rows()) == [(b"1", b"x")]


def test_repeating_a_column_is_allowed(tmp_path: Path) -> None:
    path = make_dump(tmp_path, values(b"(1,'x')"))
    assert list(open_dump(path).rows("a", "a")) == [(b"1", b"1")]


def test_unknown_column_is_an_error(tmp_path: Path) -> None:
    path = make_dump(tmp_path, values(b"(1,'x')"))
    with pytest.raises(DumpFormatError, match="no column 'nope'"):
        list(open_dump(path).rows("nope"))


# --------------------------------------------------------------------------- #
# Statement structure
# --------------------------------------------------------------------------- #


def test_multiple_statements(tmp_path: Path) -> None:
    path = make_dump(tmp_path, values(b"(1,'a')") + values(b"(2,'b')") + values(b"(3,'c')"))
    assert list(open_dump(path).rows("a")) == [(b"1",), (b"2",), (b"3",)]


def test_empty_table(tmp_path: Path) -> None:
    path = make_dump(tmp_path, b"")
    reader = open_dump(path)
    assert reader.schema.columns == ("a", "b")
    assert list(reader.rows()) == []


def test_final_statement_without_a_semicolon(tmp_path: Path) -> None:
    path = make_dump(tmp_path, b"INSERT INTO `t` VALUES (1,'a')")
    assert list(open_dump(path).rows("a")) == [(b"1",)]


def test_trailing_sql_after_the_inserts_is_ignored(tmp_path: Path) -> None:
    body = values(b"(1,'a')") + b"/*!40000 ALTER TABLE `t` ENABLE KEYS */;\nUNLOCK TABLES;\n"
    path = make_dump(tmp_path, body)
    assert list(open_dump(path).rows("a")) == [(b"1",)]


def test_wrong_value_count_is_an_error(tmp_path: Path) -> None:
    path = make_dump(tmp_path, values(b"(1,'a','extra')"))
    with pytest.raises(DumpFormatError, match="3 values"):
        list(open_dump(path).rows())


def test_explicit_column_list_is_refused(tmp_path: Path) -> None:
    path = make_dump(tmp_path, b"INSERT INTO `t` (`a`,`b`) VALUES (1,'a');\n")
    with pytest.raises(DumpFormatError, match="explicit column list"):
        list(open_dump(path).rows())


def test_insert_into_another_table_is_an_error(tmp_path: Path) -> None:
    path = make_dump(tmp_path, values(b"(1,'a')", table=b"somewhere_else"))
    with pytest.raises(DumpFormatError, match="somewhere_else"):
        list(open_dump(path).rows())


def test_truncated_mid_tuple_is_an_error(tmp_path: Path) -> None:
    path = make_dump(tmp_path, b"INSERT INTO `t` VALUES (1,'unterminated")
    with pytest.raises(DumpFormatError, match="ends inside an INSERT"):
        list(open_dump(path).rows())


def test_block_size_must_be_positive(tmp_path: Path) -> None:
    path = make_dump(tmp_path, values(b"(1,'a')"))
    with pytest.raises(ValueError, match="at least 1"):
        open_dump(path, block_size=0)


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #


def test_round_trips_arbitrary_bytes(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Whatever mysqldump could emit, the parser must give back unchanged."""
    directory = tmp_path_factory.mktemp("roundtrip_one")

    @given(st.binary(max_size=64))
    @settings(max_examples=400, deadline=None)
    def check(value: bytes) -> None:
        path = make_dump(directory, values(b"(1," + mysql_quote(value) + b")"))
        assert list(open_dump(path).rows("b")) == [(value,)]

    check()


def test_round_trips_many_rows(tmp_path_factory: pytest.TempPathFactory) -> None:
    directory = tmp_path_factory.mktemp("roundtrip_many")

    @given(st.lists(st.binary(max_size=24), min_size=1, max_size=8))
    @settings(max_examples=150, deadline=None)
    def check(blobs: list[bytes]) -> None:
        tuples = [
            b"(" + str(i).encode() + b"," + mysql_quote(blob) + b")" for i, blob in enumerate(blobs)
        ]
        path = make_dump(directory, values(*tuples))
        assert [value for (value,) in open_dump(path).rows("b")] == blobs

    check()


def test_round_trips_at_any_block_size(tmp_path_factory: pytest.TempPathFactory) -> None:
    """The two risky areas together: arbitrary escapes *and* a hostile buffer."""
    directory = tmp_path_factory.mktemp("roundtrip_blocks")

    @given(st.binary(max_size=48), st.integers(min_value=1, max_value=32))
    @settings(max_examples=200, deadline=None)
    def check(value: bytes, block_size: int) -> None:
        path = make_dump(directory, values(b"(1," + mysql_quote(value) + b")"))
        reader = open_dump(path, block_size=block_size)
        assert list(reader.rows("b")) == [(value,)]

    check()


def test_schema_survives_arbitrary_column_names(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    directory = tmp_path_factory.mktemp("schema_names")
    identifier = st.text(
        alphabet=st.characters(min_codepoint=0x61, max_codepoint=0x7A), min_size=1, max_size=12
    )

    @given(st.lists(identifier, min_size=1, max_size=6, unique=True))
    @settings(max_examples=100, deadline=None)
    def check(names: list[str]) -> None:
        lines = b",\n".join(b"  `" + n.encode() + b"` int NOT NULL" for n in names)
        ddl = (
            b"CREATE TABLE `t` (\n"
            + lines
            + b",\n  PRIMARY KEY (`"
            + names[0].encode()
            + b"`)\n) ENGINE=InnoDB;\n"
        )
        row = b"(" + b",".join(b"1" for _ in names) + b")"
        path = make_dump(directory, values(row), ddl=ddl)
        reader = open_dump(path)
        assert reader.schema.columns == tuple(names)
        assert list(reader.rows()) == [tuple(b"1" for _ in names)]

    check()
