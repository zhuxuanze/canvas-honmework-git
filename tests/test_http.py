import json

import httpx
import pytest

from canvas_sync.canvas import Canvas
from canvas_sync.errors import AuthExpired, InvalidResponse, NetworkError, PermissionDenied
from canvas_sync.http import ReadOnlyHTTP


def client(handler, **kwargs):
    return ReadOnlyHTTP("https://canvas.test", transport=httpx.MockTransport(handler), sleep=lambda _: None, **kwargs)


def test_courses_multiple_pages_and_completed():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        state = request.url.params.get("enrollment_state")
        page = int(request.url.params.get("page", "1"))
        headers = {"Link": f'<https://canvas.test/api/v1/courses?enrollment_state={state}&page={page+1}&opaque=ok>; rel="next"'} if page < 3 else {}
        return httpx.Response(200, json=[{"id": page + (10 if state == "completed" else 0), "name": "同名课程"}], headers=headers)
    http = client(handler)
    courses = list(Canvas(http).courses())
    assert len(courses) == 6
    assert len({c["id"] for c in courses}) == 6
    assert sum(c["history"] for c in courses) == 3
    assert "opaque=ok" in calls[1] and "per_page" not in calls[1]


def test_session_cookie_parent_domain_and_xssi():
    def handler(request):
        assert request.headers["Cookie"] == "session=private"
        return httpx.Response(200, text='while(1);{"id":42,"name":"学生"}')
    http = ReadOnlyHTTP("https://canvas.school.test", cookies=[{"domain": ".school.test", "name": "session", "value": "private", "path": "/", "secure": True, "expires": -1}], transport=httpx.MockTransport(handler))
    assert Canvas(http).identity()["id"] == "42"


def test_redirect_never_leaks_credentials(tmp_path):
    headers = []
    def handler(request):
        headers.append(dict(request.headers))
        if request.url.host == "canvas.test":
            return httpx.Response(302, headers={"Location": "https://cdn.test/file?signature=ok", "Set-Cookie": "other=1; Domain=canvas.test"})
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, content=b"a,b\n1,2\n")
    http = client(handler, token="secret", cookies=[{"domain": "canvas.test", "name": "session", "value": "private", "path": "/"}])
    http.download("/files/1/download", tmp_path / "f", 8, "data.csv")
    assert headers[0]["authorization"] == "Bearer secret"
    assert headers[0]["cookie"] == "session=private"


@pytest.mark.parametrize("code,exception", [(401, AuthExpired), (403, PermissionDenied), (404, PermissionDenied)])
def test_status_errors(code, exception):
    with pytest.raises(exception):
        client(lambda _: httpx.Response(code)).json("/api/v1/courses")


@pytest.mark.parametrize("body", [b'<html><form action="/login"><input type="password"></form></html>', b'<html>Sign in</html>'])
def test_login_page_never_saved(body, tmp_path):
    http = client(lambda _: httpx.Response(200, content=body, headers={"Content-Type": "text/html"}))
    with pytest.raises(AuthExpired):
        http.download("/files/1/download", tmp_path / "out", None, "data.csv")
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob("*.part"))
    with pytest.raises(AuthExpired):
        http.json("/api/v1/users/self/profile")


def test_rate_limit_and_server_recovery():
    attempts, delays = [], []
    def handler(request):
        attempts.append(request)
        return httpx.Response(429, headers={"Retry-After": "2"}) if len(attempts) == 1 else httpx.Response(503) if len(attempts) == 2 else httpx.Response(200, json=[])
    http = client(handler)
    http.sleep = delays.append
    assert http.json("/api/v1/courses")[0] == []
    assert len(attempts) == 3 and delays[0] == 2


def test_retries_are_bounded():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429)
    with pytest.raises(NetworkError):
        client(handler).json("/api/v1/courses")
    assert len(calls) == 4


def test_stream_interruption_restarts_cleanly(tmp_path):
    calls = []
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial"
            raise httpx.ReadError("interrupted")
    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=Broken()) if len(calls) == 1 else httpx.Response(200, content=b"complete")
    http = client(handler)
    http.download("/files/1", tmp_path / "out", 8, "f.txt")
    assert (tmp_path / "out").read_bytes() == b"complete"
    assert len(calls) == 2 and not list(tmp_path.glob("*.part"))


def test_download_wrong_size_and_invalid_notebook(tmp_path):
    http = client(lambda _: httpx.Response(200, content=b"abc"))
    with pytest.raises(NetworkError):
        http.download("/files/1", tmp_path / "out", 9, "f.csv")
    with pytest.raises(InvalidResponse):
        http.download("/files/1", tmp_path / "out", 3, "f.ipynb")
    assert not (tmp_path / "out").exists()


def test_cross_origin_pagination_is_rejected():
    def handler(request):
        assert request.url.host == "canvas.test"
        return httpx.Response(200, json=[1], headers={"Link": '<https://evil.test/api?page=2>; rel="next"'})
    with pytest.raises(InvalidResponse):
        list(client(handler, token="secret").paginate("/api/v1/courses"))


def test_pagination_loop_is_detected():
    http = client(lambda _: httpx.Response(200, json=[], headers={"Link": '<https://canvas.test/api?page=2>; rel="next"'}))
    with pytest.raises(InvalidResponse):
        list(http.paginate("/api"))
