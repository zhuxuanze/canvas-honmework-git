import httpx

from canvas_sync.http import ReadOnlyHTTP


def test_predictable_part_symlink_not_followed(tmp_path):
    victim = tmp_path / "outside.txt"
    victim.write_bytes(b"keep")
    destination = tmp_path / "file"
    destination.with_suffix(".part").symlink_to(victim)
    http = ReadOnlyHTTP("https://canvas.test", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"safe")))
    http.download("/files/1", destination, 4, "file.csv")
    assert destination.read_bytes() == b"safe" and victim.read_bytes() == b"keep"
