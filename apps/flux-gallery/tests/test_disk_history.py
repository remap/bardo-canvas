from flux_gallery.disk_history import save_and_prune


def test_save_and_prune_writes_file_with_correct_content(tmp_path):
    path = save_and_prune(tmp_path / "screen-f", "a.png", b"data", keep=200)
    assert path.read_bytes() == b"data"
    assert path.name == "a.png"


def test_save_and_prune_keeps_only_the_most_recent_n(tmp_path):
    directory = tmp_path / "screen-f"
    save_and_prune(directory, "a.png", b"1", keep=2)
    save_and_prune(directory, "b.png", b"2", keep=2)
    save_and_prune(directory, "c.png", b"3", keep=2)

    remaining = sorted(p.name for p in directory.iterdir())
    assert remaining == ["b.png", "c.png"]


def test_save_and_prune_does_not_prune_when_under_the_limit(tmp_path):
    directory = tmp_path / "screen-f"
    save_and_prune(directory, "a.png", b"1", keep=200)
    save_and_prune(directory, "b.png", b"2", keep=200)

    remaining = sorted(p.name for p in directory.iterdir())
    assert remaining == ["a.png", "b.png"]


def test_save_and_prune_ignores_subdirectories(tmp_path):
    directory = tmp_path / "screen-f"
    directory.mkdir()
    (directory / "aa-subdir").mkdir()  # sorts first, so it would be the prune candidate
    save_and_prune(directory, "b.png", b"1", keep=1)
    save_and_prune(directory, "c.png", b"2", keep=1)

    # The subdirectory survives untouched and never counts toward the retention limit.
    assert (directory / "aa-subdir").is_dir()
    assert sorted(p.name for p in directory.iterdir() if p.is_file()) == ["c.png"]
