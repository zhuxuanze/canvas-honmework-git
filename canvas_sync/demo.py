"""纯本地 httpx MockTransport；演示数据不会连接真实学校。"""
import base64
import json
from collections import Counter

import httpx

from .canvas import Canvas
from .http import ReadOnlyHTTP

BASE = "https://demo.canvas.invalid"


def demo_pdf():
    stream = b"BT /F1 18 Tf 50 760 Td (Demo lecture - simulated data) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(data)


class DemoServer:
    def __init__(self):
        self.calls = Counter()
        self.version = 1
        self.forbid_files = False
        self.expired = False
        self.rate_limit = 0
        self.contents = {
            "11": json.dumps({"cells": [{"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": ["import csv\n", "with open('data.csv', encoding='utf-8') as f:\n", "    rows = list(csv.DictReader(f))\n", "print(rows)\n"]}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}).encode(),
            "12": b"student,score\nAlice,85\nBob,92\n",
            "13": demo_pdf(),
            "14": "# 演示阅读材料\n线性代数与数据处理。\n".encode(),
            "15": base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j3ioAAAAASUVORK5CYII="),
            "16": b"student,score\nOther,70\n",
        }

    def file(self, rid):
        names = {"11": "analysis.ipynb", "12": "data.csv", "13": "lecture.pdf", "14": "阅读材料.txt", "15": "diagram.png", "16": "data.csv"}
        return {"id": int(rid), "filename": names[rid], "display_name": names[rid], "folder_id": 101,
                "size": len(self.contents[rid]), "updated_at": f"2026-10-{self.version+1:02d}T04:00:00Z",
                "url": BASE + f"/download/{rid}", "locked_for_user": False}

    def assignment(self):
        return {"id": 501, "name": "作业一：Python 数据分析", "due_at": "2026-10-20T15:59:00Z", "updated_at": "2026-10-01T01:00:00Z",
                "all_dates": [{"due_at": "2026-12-01T00:00:00Z", "title": "其他班级（不适用）"}],
                "description": '<h2>一、读取数据</h2><p>请保持 notebook 与 CSV 位于同一个目录。</p><ol><li>加载 data.csv</li><li>计算平均值并作图</li></ol><table><tr><th>项目</th><th>分值</th></tr><tr><td>分析过程</td><td>10</td></tr></table><p><a href="/courses/1/files/11/download">analysis.ipynb</a> <a href="/courses/1/files/12/download">data.csv</a></p><img src="/courses/1/files/15/preview" alt="示意图"><p><a href="https://example.org/reading">外部阅读链接</a>（未下载）</p>'}

    def handle(self, request):
        path = request.url.path
        self.calls[path] += 1
        if self.expired:
            return httpx.Response(200, text='<html><form action="/login"><input type="password"></form></html>', headers={"Content-Type": "text/html"})
        if self.rate_limit > 0:
            self.rate_limit -= 1
            return httpx.Response(429, headers={"Retry-After": "0"})
        if path == "/api/v1/users/self/profile":
            return httpx.Response(200, json={"id": 9001, "name": "演示学生（模拟数据）"})
        if path == "/api/v1/courses":
            state = request.url.params.get("enrollment_state", "active")
            page = request.url.params.get("page", "1")
            courses = [{"id": 1, "name": "Python 与数据科学", "term": {"id": 1, "name": "2026 秋季"}},
                       {"id": 2, "name": "线性代数", "term": {"id": 1, "name": "2026 秋季"}}]
            if state == "completed":
                return httpx.Response(200, json=[{"id": 3, "name": "大学写作", "term": {"id": 2, "name": "2026 春季"}}])
            return self.list_page(request, courses)
        if path.startswith("/download/"):
            rid = path.rsplit("/", 1)[1]
            return httpx.Response(200, content=self.contents[rid], headers={"Content-Type": "application/octet-stream"})
        if path.startswith("/api/v1/files/"):
            rid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json=self.file(rid)) if rid in self.contents else httpx.Response(404)
        if path.endswith("/folders"):
            return self.list_page(request, [{"id": 100, "name": "course files", "parent_folder_id": None}, {"id": 101, "name": "第一周", "parent_folder_id": 100}])
        if path.endswith("/files"):
            if self.forbid_files:
                return httpx.Response(403)
            return self.list_page(request, [self.file(r) for r in self.contents])
        if path.endswith("/assignments"):
            return httpx.Response(200, json=[self.assignment(), {"id": 502, "name": "作业二：无附件阅读报告", "description": "<h2>阅读与思考</h2><p>写一页自己的理解。</p>", "due_at": None}])
        if "/assignments/" in path:
            return httpx.Response(200, json=self.assignment())
        if path.endswith("/pages"):
            return self.list_page(request, [{"page_id": 601, "url": "week-one", "title": "第一周学习指南"}])
        if "/pages/" in path:
            return httpx.Response(200, json={"page_id": 601, "url": "week-one", "title": "第一周学习指南", "body": '<h2>课前阅读</h2><p><a href="/courses/1/files/14/download">阅读材料</a></p>'})
        if path.endswith("/modules"):
            return httpx.Response(200, json=[{"id": 701, "name": "第一周", "items_count": 4, "items": [{"id": 1, "type": "File", "content_id": 11}], "state": "unlocked"}])
        if path.endswith("/items"):
            return self.list_page(request, [{"id": 1, "type": "File", "content_id": 11}, {"id": 2, "type": "File", "content_id": 12}, {"id": 3, "type": "Assignment", "content_id": 501, "html_url": BASE + "/courses/1/assignments/501"}, {"id": 4, "type": "Page", "page_url": "week-one", "html_url": BASE + "/courses/1/pages/week-one"}])
        return httpx.Response(404)

    @staticmethod
    def list_page(request, items):
        page = int(request.url.params.get("page", "1"))
        offset = (page - 1) * 2
        data = items[offset:offset + 2]
        headers = {}
        if offset + 2 < len(items):
            url = str(request.url.copy_set_param("page", page + 1))
            headers["Link"] = f'<{url}>; rel="next"'
        return httpx.Response(200, json=data, headers=headers)

    def canvas(self):
        return Canvas(ReadOnlyHTTP(BASE, transport=httpx.MockTransport(self.handle), sleep=lambda _: None))
