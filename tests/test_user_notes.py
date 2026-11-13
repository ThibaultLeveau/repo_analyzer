from pathlib import Path

import pytest

from repopilot.user_notes import MAX_BYTES, NoteError, Notes, RevisionConflict


def test_revision_lifecycle_stale_updates_and_project_isolation(tmp_path: Path) -> None:
    notes = Notes(tmp_path, clock=lambda: 450.0)
    text = b"# Dispatch\nFirst delivery uses a walnut."
    old = notes.save("Owner/Repo", "dispatch.md", "text/markdown", text)
    other = notes.save("owner/repo", "dispatch.md", "text/plain", b"Unrelated content")
    assert other.id != old.id and len(notes.for_project("owner/repo")) == 2
    assert notes.search("owner/repo", "walnut")[0][0] == old
    assert not notes.search("owner/other", "walnut")
    with pytest.raises(KeyError):
        notes.read("owner/other", old.id, old.revision)
    with pytest.raises(RevisionConflict):
        notes.remove("owner/other", old.id, old.revision)
    new = notes.save(
        "owner/repo",
        "dispatch.md",
        "text/plain",
        b"## New delivery\nUse hazelnut.",
        note_id=old.id,
        expected=old.revision,
    )
    assert old.id == new.id and old.revision != new.revision
    assert notes.original("owner/repo", old.id, old.revision) == text
    assert (
        notes.save(
            "owner/repo",
            "dispatch.md",
            "text/plain",
            b"## New delivery\nUse hazelnut.",
            note_id=new.id,
            expected=new.revision,
        )
        == new
    )
    assert not notes.search("owner/repo", "walnut")
    [(hit, section)] = notes.search("owner/repo", "hazelnut")
    assert (
        hit == new and section.section == "New delivery" and section.url == "#heading-1"
    )
    assert notes.read("owner/repo", old.id, old.revision)[1][0].text.endswith("walnut.")
    with pytest.raises(RevisionConflict):
        notes.save(
            "owner/repo",
            "other.md",
            "text/markdown",
            b"stale upload",
            note_id=new.id,
            expected=old.revision,
        )
    with pytest.raises(RevisionConflict):
        notes.remove("owner/repo", new.id, old.revision)
    notes.remove("owner/repo", new.id, new.revision)
    assert notes.for_project("owner/repo") == [other] and not notes.search(
        "owner/repo", "hazelnut"
    )
    with pytest.raises(KeyError):
        notes.read("owner/repo", old.id, old.revision)


@pytest.mark.parametrize(
    ("name", "media", "data"),
    [
        ("file.txt", "text/markdown", b"note"),
        ("file.md", "application/pdf", b"not Markdown"),
        ("../file.md", "text/plain", b"no"),
        ("file.md", "text/markdown", bytes(MAX_BYTES + 1)),
        ("file.md", "text/markdown", b"bad\xff"),
        ("file.md", "text/markdown", b"a\x00b"),
        ("file.md", "text/markdown", b"   "),
    ],
)
def test_reject_invalid_upload_without_indexing(
    tmp_path: Path, name: str, media: str, data: bytes
) -> None:
    notes = Notes(tmp_path)
    with pytest.raises(NoteError):
        notes.save("owner/repo", name, media, data)
    assert notes.for_project("owner/repo") == []
