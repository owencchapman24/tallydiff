from io import StringIO

import pytest

from tallydiff import IngestionError, Source, ingest_csv, inspect_csv_columns


def test_inspection_preserves_exact_header_order_and_only_reads_the_header() -> None:
    stream = StringIO('" Vendor ID ","Invoice, Ref",amount\nINVALID DATA\n', newline="")
    assert inspect_csv_columns(stream, source=Source.A) == (" Vendor ID ", "Invoice, Ref", "amount")
    assert stream.read() == "INVALID DATA\n"
    assert not stream.closed


@pytest.mark.parametrize(
    "text", ["", "\n", "id,,amount\n", "id, ,amount\n", "id,id,amount\n", '"id,amount\n']
)
def test_inspection_and_ingestion_share_header_failures(text: str) -> None:
    with pytest.raises(IngestionError) as inspected:
        inspect_csv_columns(text, source=Source.B)
    with pytest.raises(IngestionError) as ingested:
        ingest_csv(text, source=Source.B, key_columns=["id"], amount_column="amount")
    assert str(inspected.value) == str(ingested.value)
    assert inspected.value.source is Source.B
    assert inspected.value.source_row == 1


def test_inspection_accepts_header_only_without_guessing_mappings() -> None:
    assert inspect_csv_columns("first,second\n", source=Source.A) == ("first", "second")


def test_inspection_wraps_header_read_failure() -> None:
    class BrokenStream(StringIO):
        def __next__(self) -> str:
            raise OSError("synthetic read failure")

    with pytest.raises(IngestionError, match="could not read CSV text") as error:
        inspect_csv_columns(BrokenStream(), source=Source.A)
    assert error.value.source_row == 1


def test_inspection_requires_an_explicit_source_enum() -> None:
    with pytest.raises(TypeError, match="Source enum"):
        inspect_csv_columns("id,amount", source="A")
