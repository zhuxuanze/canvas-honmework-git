from urllib.parse import quote

from .errors import InvalidResponse


class Canvas:
    def __init__(self, http):
        self.http = http
        self.base = http.base

    def identity(self):
        data, _ = self.http.json("/api/v1/users/self/profile")
        if not isinstance(data, dict) or not data.get("id"):
            raise InvalidResponse("未识别到当前 Canvas 账户")
        return {"id": str(data["id"]), "name": data.get("name", "Canvas 用户")}

    def _list(self, path, params=None, keys=("id",)):
        for item in self.http.paginate(path, params):
            if not isinstance(item, dict) or not any(item.get(key) is not None for key in keys):
                raise InvalidResponse("列表条目缺少资源标识，学校接口返回结构需要适配")
            yield item

    def _one(self, path):
        item = self.http.json(path)[0]
        if not isinstance(item, dict) or not (item.get("id") or item.get("page_id") or item.get("url")):
            raise InvalidResponse("详情接口未提供有效资源，学校接口返回结构需要适配")
        return item

    def courses(self):
        # 明确读取 active 和 completed，历史课程不依赖服务器默认过滤。
        seen = set()
        for state in ("active", "completed"):
            for course in self._list("/api/v1/courses", {"include[]": "term", "enrollment_state": state}):
                if "id" not in course or str(course["id"]) in seen:
                    continue
                seen.add(str(course["id"]))
                term = course.get("term") or {}
                yield {"id": str(course["id"]), "name": course.get("name", "未命名课程"),
                       "term": term.get("name") or "学期未提供", "term_id": term.get("id"),
                       "history": state == "completed", "source": f'{self.base}/courses/{course["id"]}'}

    def files(self, cid):
        return self._list(f"/api/v1/courses/{cid}/files")

    def folders(self, cid):
        return self._list(f"/api/v1/courses/{cid}/folders")

    def assignments(self, cid):
        return self._list(f"/api/v1/courses/{cid}/assignments")

    def pages(self, cid):
        return self._list(f"/api/v1/courses/{cid}/pages", {"include[]": "body"}, ("page_id", "url"))

    def modules(self, cid):
        return self._list(f"/api/v1/courses/{cid}/modules", {"include[]": ["items", "content_details"]})

    def items(self, cid, module):
        inline = module.get("items")
        if inline is not None and module.get("items_count") == len(inline):
            yield from inline
        else:
            # 缺失/被截断的内嵌 items 不能代表整个模块。
            yield from self._list(f'/api/v1/courses/{cid}/modules/{module["id"]}/items',
                                         {"include[]": "content_details"})

    def file(self, rid):
        return self._one(f"/api/v1/files/{rid}")

    def assignment(self, cid, rid):
        return self._one(f"/api/v1/courses/{cid}/assignments/{rid}")

    def page(self, cid, slug):
        return self._one(f"/api/v1/courses/{cid}/pages/{quote(str(slug), safe='')}")
