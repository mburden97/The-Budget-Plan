from datetime import date

import pytest

from budgetapp import documents

PDF = b"%PDF-1.7\npretend this is a scanned policy\n%%EOF"


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "documents"


def test_a_version_round_trips_and_the_file_on_disk_is_encrypted(conn, folder):
    doc = documents.add_document(conn, name="Renters policy", notes="renews in spring")
    version_id = documents.save_version(
        conn, folder, doc, filename="policy-2026.pdf", data=PDF
    )

    version, data = documents.open_version(conn, folder, version_id)
    assert data == PDF
    assert (version.filename, version.content_type) == ("policy-2026.pdf", "application/pdf")
    assert version.size_bytes == len(PDF) and version.viewable

    [blob] = list(folder.glob("*.bin"))
    on_disk = blob.read_bytes()
    assert PDF not in on_disk and b"policy" not in on_disk  # nothing readable, name included
    assert len(on_disk) == len(PDF) + documents.NONCE_BYTES + 16  # nonce + ciphertext + tag
    assert blob.name != "policy-2026.pdf"


def test_newer_versions_stack_and_the_date_moves(conn, folder):
    doc = documents.add_document(conn, name="Lease")
    first = documents.save_version(conn, folder, doc, filename="lease.pdf", data=b"first")
    second = documents.save_version(conn, folder, doc, filename="lease-renewed.pdf", data=b"second")

    [listed] = documents.list_documents(conn)
    assert listed.latest.id == second and listed.latest.filename == "lease-renewed.pdf"
    assert [v.id for v in listed.older] == [first]
    assert listed.updated_on == date.today() and listed.size_bytes == len(b"first") + len(b"second")

    documents.delete_version(conn, folder, first)
    assert documents.get_document(conn, doc).older == ()
    assert len(list(folder.glob("*.bin"))) == 1  # the deleted one's file went too


def test_deleting_a_document_takes_its_files(conn, folder):
    doc = documents.add_document(conn, name="Tax return")
    for name in ("a.pdf", "b.pdf"):
        documents.save_version(conn, folder, doc, filename=name, data=b"x" * 10)
    assert documents.total_bytes(conn) == 20

    documents.delete_document(conn, folder, doc)
    assert documents.list_documents(conn) == [] and list(folder.glob("*.bin")) == []


def test_documents_without_a_file_sort_last_and_have_no_date(conn, folder):
    empty = documents.add_document(conn, name="Warranty")
    filed = documents.add_document(conn, name="Pay stub")
    documents.save_version(conn, folder, filed, filename="stub.pdf", data=PDF)

    order = [d.name for d in documents.list_documents(conn)]
    assert order == ["Pay stub", "Warranty"]
    assert documents.get_document(conn, empty).updated_on is None


def test_bad_input_is_refused(conn, folder):
    doc = documents.add_document(conn, name="Lease")
    with pytest.raises(ValueError, match="already have a document"):
        documents.add_document(conn, name="lease")  # names are case-insensitive
    with pytest.raises(ValueError, match="name"):
        documents.add_document(conn, name="   ")
    with pytest.raises(ValueError, match="empty"):
        documents.save_version(conn, folder, doc, filename="x.pdf", data=b"")
    with pytest.raises(ValueError, match="limited to"):
        documents.save_version(
            conn, folder, doc, filename="x.pdf", data=b"x" * (documents.MAX_BYTES + 1)
        )
    with pytest.raises(LookupError):
        documents.save_version(conn, folder, 999, filename="x.pdf", data=PDF)
    assert list(folder.glob("*.bin")) == []  # nothing was written for any of those


def test_filenames_cannot_escape_the_folder(conn, folder):
    doc = documents.add_document(conn, name="Statement")
    version_id = documents.save_version(
        conn, folder, doc, filename=r"..\..\windows\system32\evil.txt", data=b"hi"
    )
    version, _ = documents.open_version(conn, folder, version_id)
    assert version.filename == "evil.txt"
    assert documents.safe_filename("/etc/passwd") == "passwd"
    assert documents.safe_filename("  ..  ") == "document"


def test_a_damaged_file_is_reported_not_returned(conn, folder):
    doc = documents.add_document(conn, name="Statement")
    version_id = documents.save_version(conn, folder, doc, filename="s.pdf", data=PDF)
    [blob] = list(folder.glob("*.bin"))
    blob.write_bytes(blob.read_bytes()[:-1] + b"\x00")  # flip the last byte of the tag
    with pytest.raises(LookupError, match="damaged"):
        documents.open_version(conn, folder, version_id)

    blob.unlink()
    with pytest.raises(LookupError, match="missing"):
        documents.open_version(conn, folder, version_id)


def test_orphan_files_are_cleaned_up(conn, folder):
    doc = documents.add_document(conn, name="Statement")
    documents.save_version(conn, folder, doc, filename="s.pdf", data=PDF)
    (folder / "deadbeef.bin").write_bytes(b"left over from a crash")

    assert documents.prune_orphans(conn, folder) == 1
    assert len(list(folder.glob("*.bin"))) == 1
    assert documents.prune_orphans(conn, folder) == 0


def test_unknown_types_are_stored_but_not_shown_in_the_browser():
    assert documents.content_type("statement.PDF") == "application/pdf"
    assert documents.content_type("archive.7z") == documents.DEFAULT_TYPE
    assert documents.content_type("noextension") == documents.DEFAULT_TYPE
