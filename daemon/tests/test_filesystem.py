import os

import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.filesystem import FilesystemBrowser

P = "/api/v1/filesystem"


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    return root


@pytest.fixture
def api(tmp_path, root):
    with TestClient(create_app(Settings(tmp_path / "filesystem.db", media_browser_roots=(root,)))) as client:
        yield client


def browse(api, path, mode="folder"):
    return api.get(P + "/browse", params={"path": path, "mode": mode})


def test_locations_include_dynamic_default_and_configured_roots(api, root):
    locations = api.get(P + "/locations").json()["locations"]
    assert [(location["name"], location["path"]) for location in locations[:3]] == [
        ("Mounted storage", "/mnt"), ("Removable media", "/media"), ("Local media", "/srv"),
    ]
    extra = next(location for location in locations if location["path"] == str(root))
    assert extra == {"name": "media", "path": str(root), "available": True}
    root.rmdir()
    assert next(location for location in api.get(P + "/locations").json()["locations"]
                if location["path"] == str(root))["available"] is False


def test_default_roots_do_not_depend_on_real_mounts():
    locations = FilesystemBrowser().locations()["locations"]
    assert [location["path"] for location in locations] == ["/mnt", "/media", "/srv"]
    assert all(isinstance(location["available"], bool) for location in locations)


def test_filesystem_root_cannot_be_configured(tmp_path):
    with pytest.raises(ValueError, match="must not be the filesystem root"):
        Settings(tmp_path / "filesystem.db", media_browser_roots=("/",))


def test_folder_mode_filters_hidden_and_files_with_natural_directory_sorting(api, root):
    for name in ("Album10", "album2", "Alpha", "2025", ".secret"):
        (root / name).mkdir()
    (root / "song.m3u").write_text("not returned")
    response = browse(api, root)
    assert response.status_code == 200
    assert response.json() == {
        "path": str(root.resolve()), "root": str(root), "can_go_up": False,
        "entries": [
            {"name": "2025", "type": "directory"},
            {"name": "album2", "type": "directory"},
            {"name": "Album10", "type": "directory"},
            {"name": "Alpha", "type": "directory"},
        ],
    }


def test_playlist_mode_lists_directories_and_case_insensitive_playlist_files(api, root):
    (root / "Archive").mkdir()
    (root / "favorites.M3U8").write_text("#EXTM3U")
    (root / "morning.m3u").write_text("#EXTM3U")
    (root / "audio.mp3").write_text("not returned")
    (root / ".hidden.m3u").write_text("not returned")
    response = browse(api, root, "playlist")
    assert response.status_code == 200
    assert response.json()["entries"] == [
        {"name": "Archive", "type": "directory"},
        {"name": "favorites.M3U8", "type": "file"},
        {"name": "morning.m3u", "type": "file"},
    ]


def test_child_navigation_and_parent_boundary(api, root):
    child = root / "child"
    child.mkdir()
    result = browse(api, child).json()
    assert result["root"] == str(root) and result["can_go_up"] is True
    assert browse(api, root / "..").status_code == 403


@pytest.mark.parametrize("path_builder", [
    lambda root: root / "..",
    lambda root: root.parent / "outside",
])
def test_traversal_and_outside_paths_are_rejected(api, root, path_builder):
    outside = root.parent / "outside"
    outside.mkdir(exist_ok=True)
    response = browse(api, path_builder(root))
    assert response.status_code == 403
    assert response.json()["error"] == "PATH_NOT_ALLOWED"


def test_symlink_escape_is_rejected(api, root):
    outside = root.parent / "outside"
    outside.mkdir()
    escaped = root / "system"
    try:
        escaped.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted on this host")
    response = browse(api, escaped)
    assert response.status_code == 403 and response.json()["error"] == "PATH_NOT_ALLOWED"
    assert "system" not in [entry["name"] for entry in browse(api, root).json()["entries"]]


def test_unavailable_and_unreadable_paths_return_bounded_errors(api, root, monkeypatch):
    unavailable = browse(api, root / "missing")
    assert unavailable.status_code == 404 and unavailable.json()["error"] == "PATH_UNAVAILABLE"
    monkeypatch.setattr(os, "scandir", lambda path: (_ for _ in ()).throw(PermissionError()))
    unreadable = browse(api, root)
    assert unreadable.status_code == 403 and unreadable.json()["error"] == "PATH_UNREADABLE"


def test_refresh_reads_current_contents_and_never_returns_file_contents(api, root):
    first = browse(api, root, "playlist")
    assert first.status_code == 200 and first.json()["entries"] == []
    (root / "new.m3u").write_text("private playlist contents")
    second = browse(api, root, "playlist")
    assert second.json()["entries"] == [{"name": "new.m3u", "type": "file"}]
    assert "private playlist contents" not in second.text
