import json
from pathlib import Path
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from canvas_sync.browser_login import launch, load_cookies, session_dir
from canvas_sync.canvas import Canvas
from canvas_sync.config import Config, home_dir, normalize_base, profile_id, read_token, save_token
from canvas_sync.content import date_text
from canvas_sync.demo import BASE, DemoServer
from canvas_sync.engine import SyncEngine
from canvas_sync.errors import SyncError
from canvas_sync.http import ReadOnlyHTTP
from canvas_sync.platform import open_local
from canvas_sync.store import Store
from canvas_sync.cleanup import Cleanup
from canvas_sync.index import existing_path
from canvas_sync.tasks import Tasks, STATUSES, effective_due, utc_time
from canvas_sync.reminders import (MacNotifier, center, mark_seen, remaining, service_status,
                                   settings as reminder_settings, set_settings, start_service, stop_service)

st.set_page_config(page_title="Canvas 课程资料同步器", page_icon="📚", layout="wide")

home = home_dir()
store = Store(home / "state.sqlite3")
config = Config.load(home)
if "demo" not in st.session_state:
    st.session_state.demo = False


def canvas_connection():
    if st.session_state.demo:
        if "demo_server" not in st.session_state:
            st.session_state.demo_server = DemoServer()
        return st.session_state.demo_server.canvas()
    token = None
    cookies = []
    if config.auth_mode == "token":
        memory = st.session_state.get("token_session", {})
        token = memory.get("token") if memory.get("base") == config.base_url else read_token(config.base_url)
        if not token:
            raise SyncError("请到连接配置输入本次使用的 Token，或读取系统钥匙串中的 Token")
    else:
        cookies = load_cookies(home, config.base_url)
        if not cookies:
            raise SyncError("请到连接配置打开浏览器并完成登录")
    return Canvas(ReadOnlyHTTP(config.base_url, token, cookies))


ns = profile_id(BASE, "9001") if st.session_state.demo else config.profile
root = str(home / "演示资料") if st.session_state.demo else config.output_dir
courses = store.courses(ns) if ns else []
resources = store.resources(ns) if ns else []
last_run = store.last_run(ns) if ns else None
course_map = {str(c["id"]): c for c in courses}

with st.sidebar:
    st.markdown("### 📚 Canvas 资料库")
    st.caption("先看本地，再按需同步")
    page = st.radio("导航", ["课程与同步", "课程管理", "任务清单", "提醒中心", "资料库", "作业要求", "同步记录", "连接配置"], label_visibility="collapsed", key="navigation")
    st.divider()
    if st.session_state.demo:
        st.info("演示模式 · 模拟数据\n\n不会连接真实学校")
    elif config.profile:
        st.caption(f"当前账户：{config.user_name}\n\n{config.base_url}")
    else:
        st.caption("尚未配置真实 Canvas")
    st.caption("默认时区：Asia/Shanghai")
    if st.button("退出演示" if st.session_state.demo else "体验演示数据", width="stretch"):
        st.session_state.demo = not st.session_state.demo
        st.session_state.pop("sync_result", None)
        st.rerun()


def error_message(error):
    if isinstance(error, (SyncError, ValueError)):
        st.error(str(error))
    else:
        st.error("本地操作失败，请检查目录权限、磁盘空间和依赖安装。详情可在测试中定位；认证信息不会写入日志。")


def run_sync(selected):
    archived = {c["id"] for c in store.courses(ns) if c.get("archived")}
    selected = [c for c in selected if str(c["id"]) not in archived]
    if not selected:
        st.warning("请先勾选至少一门课程")
        return
    bar, current = st.progress(0), st.empty()
    def progress(event):
        current.info(f'{event["course"]} · {event["message"]}')
        if event["total"]:
            bar.progress(min(1., event["done"] / event["total"]))
    api = None
    try:
        api = canvas_connection()
        identity = api.identity()
        expected = "9001" if st.session_state.demo else config.user_id
        if str(identity["id"]) != expected:
            raise SyncError("当前认证账户与本地资料库不同，请在连接配置重新测试并切换账户，避免资料混用")
        engine = SyncEngine(api, store, ns, root, config.concurrency, config.timezone, progress)
        result = engine.sync(selected)
        bar.progress(1.)
        if result["auth_expired"]:
            st.error("会话已失效，请重新登录；已完成的资料仍可离线查看")
        elif result["incomplete"]:
            st.warning("同步已结束，部分来源或资源未获取完整。请查看同步记录。")
        else:
            st.success("本次已获取的入口与资源处理完成")
        st.session_state.sync_result = result
    except Exception as exc:
        error_message(exc)
    finally:
        if api:
            api.http.close()


def render_results(result):
    labels = {"added": "新增", "updated": "更新", "skipped": "跳过", "failed": "资源失败", "denied": "资源无权限", "local_modified": "本地修改冲突"}
    cols = st.columns(len(labels))
    for col, (key, label) in zip(cols, labels.items()):
        col.metric(label, result.get(key, 0))
    st.caption(f'实际网络文件下载：{result.get("network_downloads", 0)}；未下载链接：{result.get("not_downloaded", 0)}；未完成来源：{result.get("source_problems", 0)}。计数针对文件和离线文档，多个本地副本不重复计入新增。')


def source_table():
    sources = store.sources(ns) if ns else []
    if sources:
        labels = {"success": "列表完整获取", "partial": "部分获取", "denied": "无权限 / 未解锁", "failed": "获取失败", "auth_expired": "认证失效", "fetching": "未完成 / 正在获取"}
        st.dataframe(pd.DataFrame([{"课程": course_map.get(s["course"], {}).get("name", s["course"]), "来源": s["name"],
                "获取情况": labels.get(s["status"], s["status"]), "说明": s["detail"], "获取时间": date_text(s["fetched"])} for s in sources]), hide_index=True, width="stretch")
        st.caption("入口状态表示列表/API 的获取情况；附件下载和正文保存的问题另见资源状态。未列出的来源表示尚未同步。")


def resource_view(rows, assignments=False):
    if not rows:
        st.info("本地还没有相关资料。到课程与同步页面选择课程后同步，或先体验演示数据。")
        return
    a, b, c = st.columns([2, 1.5, 3])
    cid = a.selectbox("课程", ["全部"] + list(course_map), format_func=lambda x: "全部课程" if x == "全部" else course_map[x]["name"] + f" [{x}]")
    kind = b.selectbox("类型", ["全部", "file", "assignment", "page", "module_item"], format_func=lambda x: {"全部": "全部类型", "file": "附件", "assignment": "作业", "page": "课程页面", "module_item": "模块项目"}[x])
    query = c.text_input("搜索名称", placeholder="例如 notebook、CSV、作业一")
    filtered = [r for r in rows if (cid == "全部" or r["course_id"] == cid) and (kind == "全部" or r["kind"] == kind) and query.casefold() in (r.get("title", "") + r.get("filename", "")).casefold()]
    status_labels = {"added": "已新增", "updated": "已更新", "skipped": "未变化", "failed": "失败", "denied": "无权限", "local_modified": "保留本地修改", "cleared": "本地资料已清理"}
    st.dataframe(pd.DataFrame([{"课程": course_map.get(r["course_id"], {}).get("name", r["course_id"]), "名称": r.get("title"),
        "类型": {"file": "附件", "assignment": "作业", "page": "课程页面", "module_item": "模块项目"}.get(r["kind"], r["kind"]), "截止时间": date_text(r.get("due")) if r["kind"] == "assignment" else "", "状态": status_labels.get(r.get("status"), r.get("status")),
        "内容获取时间": date_text(r.get("fetched")), "说明": r.get("reason", "")} for r in filtered]), hide_index=True, width="stretch")
    if not filtered:
        return
    picked = st.selectbox("选择资料查看", range(len(filtered)), format_func=lambda i: f'{filtered[i].get("title")} [{filtered[i]["course_id"]}/{filtered[i]["id"]}]')
    row = filtered[picked]
    st.caption(f'本地路径：{row.get("path", "未保存")}\n\n远程更新时间：{date_text(row.get("updated")) if row.get("updated") else "Canvas 未提供"}；本地保存时间：{date_text(row.get("synced")) if row.get("synced") else "未成功保存"}')
    controls = st.columns(3)
    path = row.get("path") if existing_path(store, ns, row.get("path"), row["course_id"]) else None
    if not path:
        st.info("本地文件不存在或已清理。作业摘要可在任务清单查看；附件需要重新选择课程并同步。")
    if controls[0].button("打开本地文件", disabled=not path):
        try:
            open_local(path, root)
        except Exception as exc:
            error_message(exc)
    if controls[1].button("打开所在文件夹", disabled=not path):
        try:
            open_local(str(Path(path).parent), root)
        except Exception as exc:
            error_message(exc)
    if row.get("source"):
        controls[2].link_button("打开 Canvas 原页面", row["source"])
    if row.get("remote_path"):
        st.warning("此文件存在本地修改。原文件已保留，远程版本另存；作业/模块附件组合在其目录的 _Canvas远程组合 中。")
        st.code(row["remote_path"], language=None)
    if row.get("locations") and len(row["locations"]) > 1:
        with st.expander("此附件的本地副本位置"):
            st.text("\n".join(row["locations"]))
    if path and Path(path).is_file():
        p = Path(path).resolve()
        if p.is_relative_to(Path(root).expanduser().resolve()):
            if row["kind"] in {"assignment", "page"}:
                # 预览副本把本地小图片转为 data URL；链接仍由离线文件在新窗口中打开。
                from bs4 import BeautifulSoup
                import base64
                import mimetypes
                from urllib.parse import unquote
                soup = BeautifulSoup(p.read_text("utf-8"), "html.parser")
                from urllib.parse import urlsplit
                for link in soup.find_all("a", href=True):
                    if not urlsplit(link["href"]).scheme:
                        del link["href"]
                        link["title"] = "本地附件，请用打开所在文件夹查看"
                for image in soup.find_all("img"):
                    src = image.get("src", "")
                    candidate = (p.parent / unquote(src)).resolve()
                    if src and candidate.is_relative_to(Path(root).expanduser().resolve()) and candidate.is_file() and candidate.stat().st_size <= 5 * 1024 * 1024:
                        mime = mimetypes.guess_type(candidate)[0] or "image/png"
                        image["src"] = f'data:{mime};base64,' + base64.b64encode(candidate.read_bytes()).decode()
                st.caption("离线要求预览；完整附件请用“打开本地文件”查看")
                with st.container(height=620):
                    st.html(str(soup.body or soup))  # 不把离线文件的全局 CSS 注入应用。
            elif p.suffix.lower() in {".txt", ".csv", ".py", ".ipynb"}:
                with p.open("rb") as stream:
                    preview = stream.read(50000).decode("utf-8", errors="replace")
                st.code(preview, language="json" if p.suffix == ".ipynb" else "text")
                st.caption("仅显示前 50 KB，不执行脚本或 notebook")


if st.session_state.demo and page != "课程与同步":
    st.warning("演示数据 · 当前账户、课程、文件和截止时间均为模拟，不连接真实学校")


def course_label(cid):
    if cid == "personal":
        return "个人任务"
    course = course_map.get(str(cid), {})
    return f'{course.get("name", cid)} [{cid}]' + ("（归档）" if course.get("archived") else "")


def cleanup_table(report):
    labels = {"trash": "拟移入废纸篓", "keep": "保留", "blocked": "不能处理", "missing": "仅修正记录"}
    results = {"trashed": "已移入废纸篓", "kept": "已保留", "failed": "失败", "missing": "已修正缺失记录", "processing": "中断 / 结果待核对"}
    categories = {"directory": "工具创建目录", "file": "资料副本", "document": "离线要求/页面", "history": "历史版本", "remote": "远程副本", "snapshot": "远程配套组合", "index": "课程索引", "unknown": "归属未确认"}
    st.dataframe(pd.DataFrame([{"课程": course_label(i["course"]), "路径": i["path"], "类别": categories.get(i["category"], i["category"]),
            "大小（字节）": i["size"], "处理": results.get(i["result"], labels.get(i["action"], i["action"])),
            "说明": i.get("result_reason") or i.get("reason", "")} for i in report["items"]]), hide_index=True, width="stretch", alt="课程清理项目及处理原因")


def local_plan(date, clock_time):
    return datetime.combine(date, clock_time, ZoneInfo("Asia/Shanghai")).isoformat()

if page == "连接配置":
    st.title("连接你的 Canvas")
    st.write("填写学校 Canvas 根地址和资料目录。登录信息仅留在这台电脑。")
    if st.session_state.demo:
        st.warning("目前处于演示模式。退出演示后可配置真实账户。")
    base = st.text_input("Canvas 根地址", value=config.base_url, placeholder="https://canvas.example.edu")
    output = st.text_input("资料保存目录（绝对路径）", value=config.output_dir)
    mode = st.radio("认证方式", ["token", "browser"], index=0 if config.auth_mode == "token" else 1,
                    format_func=lambda x: "个人 Access Token" if x == "token" else "打开浏览器登录", horizontal=True)
    token = ""
    persistence = "仅本次使用"
    if mode == "token":
        st.caption("如果学校允许：Canvas → 账户 → 设置 → 已批准的集成 → 新建访问令牌。学校未开放时使用浏览器登录。")
        token = st.text_input("Access Token", type="password", key="token_input", help="不会写入配置、SQLite 或日志")
        persistence = st.radio("Token 保存方式", ["仅本次使用", "系统钥匙串"], horizontal=True)
    else:
        st.caption("会启动独立配置目录的 Chromium，或已安装的 Chrome/Edge。请手动完成学校 SSO、验证码和二次验证。验证成功后浏览器会关闭；Cookie 和浏览器配置仅保存在本机私有目录。")
        if st.button("打开浏览器登录"):
            try:
                launch(home, normalize_base(base))
                st.success("登录浏览器已请求启动，请完成学校登录后刷新登录状态")
            except Exception as exc:
                error_message(exc)
        if base:
            try:
                status_file = session_dir(home, normalize_base(base)) / "status.json"
                if status_file.exists():
                    status = json.loads(status_file.read_text("utf-8"))
                    st.info(status["message"])
            except ValueError:
                pass
        st.button("刷新登录状态")
    if st.button("测试连接并保存配置", type="primary"):
        connection = None
        try:
            real_base = normalize_base(base)
            target = Path(output).expanduser()
            if not target.is_absolute():
                raise ValueError("资料保存目录必须是绝对路径")
            real_token = token or read_token(real_base) if mode == "token" else None
            if mode == "token" and not real_token:
                raise ValueError("请填写 Token；如果学校未开放 Token，请选择浏览器登录")
            cookies = load_cookies(home, real_base) if mode == "browser" else []
            if mode == "browser" and not cookies:
                raise ValueError("浏览器会话尚未通过 API 验证，请先完成登录")
            connection = Canvas(ReadOnlyHTTP(real_base, real_token, cookies))
            identity = connection.identity()
            target.mkdir(parents=True, exist_ok=True)
            if mode == "token" and persistence == "系统钥匙串":
                save_token(real_base, real_token)
            if real_token:
                st.session_state.token_session = {"base": real_base, "token": real_token}
            new_config = Config(base_url=real_base, output_dir=str(target), auth_mode=mode,
                                profile=profile_id(real_base, identity["id"]), user_name=identity["name"], user_id=identity["id"])
            new_config.save(home)
            st.success(f'连接成功：{identity["name"]}（用户 ID {identity["id"]}）。请进入课程与同步刷新课程列表。')
        except Exception as exc:
            error_message(exc)
        finally:
            if connection:
                connection.http.close()
    st.caption(f"私有配置与会话目录：{home}")

elif page == "课程与同步":
    st.title("你的课程资料，随时可看")
    st.write("启动先显示本地缓存。选择课程后，一次获取作业、课件与阅读材料。")
    if st.session_state.demo:
        st.warning("演示数据 · 所有课程、账户和文件均为模拟，用于验证界面与同步流程")
    metrics = st.columns(3)
    metrics[0].metric("本地课程", len(courses))
    metrics[1].metric("缓存资料", len(resources))
    metrics[2].metric("上次同步", date_text(last_run["ended"]) if last_run and last_run["ended"] else "尚未完成")
    if not ns:
        st.info("首次使用请到“连接配置”填写地址并测试连接；也可点击侧栏“体验演示数据”。")
    if st.button("联网刷新课程列表", disabled=not ns):
        api = None
        try:
            api = canvas_connection()
            identity = api.identity()
            if profile_id(api.base, identity["id"]) != ns:
                raise SyncError("当前登录账户已改变，请先重新测试连接")
            count = 0
            for course in api.courses():
                store.save_course(ns, course)
                count += 1
            st.success(f"完整读取 {count} 门课程（含当前与历史）")
            st.rerun()
        except Exception as exc:
            error_message(exc)
            st.warning("课程列表可能不完整；已读取的课程已缓存，再次刷新会继续完整扫描")
        finally:
            if api:
                api.http.close()
    filters = st.columns([3, 1])
    search = filters[0].text_input("搜索课程", placeholder="课程名称、学期或 ID")
    period = filters[1].selectbox("范围", ["全部课程", "当前课程", "历史课程"])
    show_archived = st.checkbox("显示归档课程", key="sync_show_archived")
    if st.button("清理本地资料 / 管理归档课程", on_click=lambda: st.session_state.update(navigation="课程管理")):
        pass
    visible = [c for c in courses if (show_archived or not c.get("archived")) and search.casefold() in f'{c["name"]} {c.get("term_override") or c["term"]} {c["id"]}'.casefold() and (period == "全部课程" or c["history"] == (period == "历史课程"))]
    if visible:
        selected_ids = {c["id"] for c in courses if c["selected"]}
        hidden = selected_ids - {c["id"] for c in visible}
        st.caption("勾选下表课程后同步；搜索不会取消隐藏课程的已有选择。")
        columns = st.columns(3)
        if columns[0].button("全选当前结果"):
            store.select(ns, hidden | {c["id"] for c in visible})
            st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
            st.rerun()
        if columns[1].button("取消当前结果选择"):
            store.select(ns, hidden)
            st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
            st.rerun()
        df = pd.DataFrame([{"同步": c["selected"], "课程": c["name"], "学期": c.get("term_override") or c["term"], "范围": "历史" if c["history"] else "当前", "课程 ID": c["id"]} for c in visible])
        edited = st.data_editor(df, hide_index=True, disabled=["课程", "学期", "范围", "课程 ID"], width="stretch", key=f'courses-{ns}-{search}-{period}-{st.session_state.get("editor_version", 0)}')
        chosen = hidden | set(edited.loc[edited["同步"], "课程 ID"])
        action = st.columns(2)
        if action[0].button("记住课程选择"):
            store.select(ns, chosen)
            st.success("已保存选择，下次启动会恢复")
        if action[1].button(f"同步选中课程（{len(chosen)} 门）", type="primary"):
            store.select(ns, chosen)
            run_sync([c for c in courses if c["id"] in chosen])
        single = st.selectbox("单门课程 / 手动学期", [c["id"] for c in visible], format_func=lambda cid: course_map[cid]["name"] + f" [{cid}]")
        controls = st.columns(3)
        if controls[0].button("同步这门课程", disabled=course_map[single].get("archived", False)):
            run_sync([course_map[single]])
        if controls[1].button("打开资料保存目录"):
            try:
                open_local(root, root)
            except Exception as exc:
                error_message(exc)
        controls[2].link_button("打开 Canvas 课程", course_map[single]["source"])
        with st.expander("设置此课程的本地学期"):
            manual = st.text_input("本地学期名称", value=course_map[single].get("term_override", ""), help="留空恢复 Canvas 学期；修改后下次同步写入新目录，旧资料保留")
            if st.button("保存学期"):
                store.term(ns, single, manual.strip())
                st.rerun()
    elif ns:
        st.info("没有匹配的缓存课程。首次使用请联网刷新课程列表。")
    if st.session_state.get("sync_result"):
        render_results(st.session_state.sync_result)
    source_table()

elif page == "课程管理":
    st.title("课程管理与本地清理")
    st.info("只处理同步器管理的本地资料，不删除或修改 Canvas 内容。用户自建文件、修改过的文件组和共享缓存默认保留。")
    if not courses:
        st.info("请先配置 Canvas 并刷新课程列表，或进入演示模式。")
    else:
        cleaner = Cleanup(store)
        selected = st.multiselect("选择要清理的课程", list(course_map), format_func=course_label, key=f"cleanup_courses:{ns}")
        archive = st.checkbox("同时归档课程：停止同步与提醒，保留任务历史", key=f"cleanup_archive:{ns}")
        selection_stamp = (ns, root, tuple(sorted(selected)), archive)
        if st.session_state.get("cleanup_stamp") != selection_stamp:
            st.session_state.pop("cleanup_preview", None)
            st.session_state.cleanup_stamp = selection_stamp
        if st.button("预览清理范围", disabled=not selected):
            try:
                with st.spinner("核对文件归属、内容和目录…"):
                    report = cleaner.preview(ns, selected, root, archive=archive)
                st.session_state.cleanup_preview = report["id"]
                st.session_state.pop("cleanup_result", None)
            except Exception as exc:
                error_message(exc)
        if st.session_state.get("cleanup_preview"):
            preview = cleaner.report(ns, st.session_state.cleanup_preview)
            st.subheader("执行前预览")
            st.write(f'拟处理 {preview["file_count"]} 个文件，约 {preview["bytes"]/1024/1024:.2f} MB。共享下载缓存不纳入此次清理。')
            cleanup_table(preview)
            groups = preview["protected_groups"]
            if groups:
                chosen_groups = st.multiselect("明确选择一并清理的托管文件组", groups, key=f'cleanup_groups:{preview["id"]}',
                    help="会包括已修改文件及配套附件。用户新增文件仍保留，但其引用的托管材料可能被移走。")
                if st.button("按所选文件组更新预览", disabled=not chosen_groups):
                    try:
                        updated = cleaner.preview(ns, selected, root, chosen_groups, archive)
                        st.session_state.cleanup_preview = updated["id"]
                        st.rerun()
                    except Exception as exc:
                        error_message(exc)
            confirmed = st.checkbox("我已核对上述范围，确认仅将拟处理的本地文件移入系统废纸篓", key=f'cleanup_confirm:{preview["id"]}')
            if st.button("确认执行清理", type="primary", disabled=not confirmed or preview["status"] != "preview"):
                try:
                    with st.spinner("逐项移动到系统废纸篓，记录处理结果…"):
                        result = cleaner.execute(ns, preview["id"])
                    st.session_state.cleanup_result = result["id"]
                    st.session_state.pop("cleanup_preview", None)
                    st.rerun()
                except Exception as exc:
                    error_message(exc)
        if st.session_state.get("cleanup_result"):
            try:
                result = cleaner.report(ns, st.session_state.cleanup_result)
                st.subheader("清理结果")
                count = {s: sum(i["result"] == s for i in result["items"]) for s in ("trashed", "kept", "failed", "missing")}
                st.write(f'已移入废纸篓 {count["trashed"]} 项；保留 {count["kept"]} 项；失败 {count["failed"]} 项；修正缺失记录 {count["missing"]} 项。已取消所选课程的同步勾选。')
                cleanup_table(result)
                if count["failed"]:
                    st.warning("部分项目未处理。请查看原因，重新预览后再确认重试；不会改为永久删除。")
                    if st.button("重新预览失败课程"):
                        retry = cleaner.preview(ns, json.loads(result["courses"]), root)
                        st.session_state.cleanup_preview = retry["id"]
                        st.rerun()
            except ValueError:
                pass
        with st.expander("归档与恢复课程"):
            cid = st.selectbox("管理课程", list(course_map), format_func=course_label)
            if st.button("取消归档" if course_map[cid].get("archived") else "归档此课程"):
                store.archive_course(ns, cid, not course_map[cid].get("archived"))
                st.rerun()
            st.caption("取消归档后可重新勾选并同步；仅恢复未来提醒，不补发归档期间的节点。")
        with st.expander("最近清理记录（包括中断的操作）"):
            runs = cleaner.recent(ns)
            if runs:
                run_id = st.selectbox("查看清理记录", [r["id"] for r in runs], format_func=lambda rid: next(r["created"] + " · " + r["status"] for r in runs if r["id"] == rid))
                cleanup_table(cleaner.report(ns, run_id))
                st.caption("中断的操作请重新预览：程序会核对文件现状，不直接重复执行旧预览。")

elif page == "任务清单":
    st.title("任务清单")
    st.caption("状态与备注只保存在本机。下载、打开或逾期都不会自动完成任务。")
    if not ns:
        st.info("请先配置 Canvas 账户，或体验演示数据。")
    else:
        task_store = Tasks(store)
        with st.container(horizontal=True):
            cid = st.selectbox("所属课程", ["全部", "personal"] + list(course_map), format_func=lambda c: "全部课程" if c == "全部" else course_label(c), key="task_course")
            status = st.selectbox("任务状态", ["全部"] + list(STATUSES), key="task_status")
            search = st.text_input("搜索任务标题", key="task_search")
        view = st.segmented_control("快捷视图", ["全部", "今天到期", "未来 7 天", "已逾期", "无日期"], default="全部", key="task_view") or "全部"
        show_completed = st.checkbox("展开已完成任务", key="task_completed")
        show_archived = st.checkbox("显示归档任务与课程", key="task_archived")
        listed = task_store.list(ns, None if cid == "全部" else cid, None if status == "全部" else status, search, view,
                                 show_completed or status == "已完成", show_archived)
        st.dataframe(pd.DataFrame([{"课程": course_label(t["course"]), "任务": t["title"], "状态": t["status"],
            "日期类型": "Canvas DDL" if t["canvas_due"] else "本地计划" if t["planned_due"] else "无日期",
            "日期": date_text(effective_due(t)) if effective_due(t) else "未设置", "归档": bool(t["archived"] or t["course_archived"])} for t in listed]),
            hide_index=True, width="stretch", alt="按日期排序的本地任务清单")
        if listed:
            picked = st.selectbox("查看或编辑任务", [t["id"] for t in listed], format_func=lambda tid: next(course_label(t["course"]) + " · " + t["title"] for t in listed if t["id"] == tid), key="task_pick")
            task = task_store.get(ns, picked)
            st.subheader(task["title"])
            st.caption(f'Canvas 原始截止时间：{task["canvas_due_raw"] or "未设置"}；显示时区：Asia/Shanghai')
            if task["date_error"]:
                st.warning(task["date_error"])
            planned = utc_time(task["planned_due"])
            local = planned.astimezone(ZoneInfo("Asia/Shanghai")) if planned else datetime.now(ZoneInfo("Asia/Shanghai"))
            with st.form(f"edit_task:{picked}"):
                new_title = st.text_input("任务标题", value=task["title"], disabled=task["origin"] == "canvas")
                new_status = st.selectbox("本地状态", STATUSES, index=STATUSES.index(task["status"]))
                notes = st.text_area("本地备注", value=task["notes"])
                archived = st.checkbox("归档此任务", value=bool(task["archived"]))
                use_plan = st.checkbox("设置本地计划时间（仅适用于无 Canvas DDL 的任务）", value=bool(planned), disabled=bool(task["canvas_due"]))
                plan_date = st.date_input("计划日期", value=local.date(), disabled=bool(task["canvas_due"]))
                plan_time = st.time_input("计划时间 · Asia/Shanghai", value=local.time().replace(tzinfo=None) if planned else time(23, 59), step=60, disabled=bool(task["canvas_due"]))
                saved = st.form_submit_button("保存本地任务", type="primary")
            if saved:
                try:
                    new_plan = task["planned_due"] if task["canvas_due"] else local_plan(plan_date, plan_time) if use_plan else None
                    task_store.edit(ns, picked, new_status, notes, new_plan, archived, new_title if task["origin"] == "manual" else None)
                    st.rerun()
                except Exception as exc:
                    error_message(exc)
            st.markdown("#### 作业要求摘要")
            st.text(task["summary"] or "此手动任务没有 Canvas 要求，可在本地备注中记录。")
            if task["requirement_html"]:
                with st.expander("完整离线要求（保留题目结构和表格）"):
                    from bs4 import BeautifulSoup
                    requirement = BeautifulSoup(task["requirement_html"], "html.parser")
                    st.html(str(requirement.body or requirement))
            if task["source"]:
                st.link_button("打开 Canvas 作业原页面", task["source"])
            for attachment in task_store.attachments(task):
                with st.container(horizontal=True):
                    st.write(attachment["title"] + ("" if attachment["available"] else " · 本地附件缺失，需要重新同步"))
                    if st.button("打开附件位置", disabled=not attachment["available"], key=f'attachment:{picked}:{attachment["id"]}'):
                        try:
                            record = store.artifact(ns, attachment["path"])
                            open_local(str(Path(attachment["path"]).parent), record["root"])
                        except Exception as exc:
                            error_message(exc)
            changes = task_store.history(ns, picked)
            if changes:
                with st.expander("截止时间改期记录"):
                    st.dataframe(pd.DataFrame([{"原日期": c["before_due"] or "无日期", "新日期": c["after_due"] or "无日期", "变更时间": date_text(c["changed"])} for c in changes]), hide_index=True, alt="任务截止时间变更记录")
        else:
            st.info("没有符合条件的待办。已完成和归档的记录可用上方选项展开。")
        with st.expander("手动添加复习、阅读或其他任务"):
            with st.form("add_manual_task", clear_on_submit=True):
                title = st.text_input("新任务标题")
                course = st.selectbox("新任务所属课程", ["personal"] + list(course_map), format_func=course_label)
                notes = st.text_area("新任务备注")
                use_plan = st.checkbox("为新任务设置本地计划时间")
                plan_date = st.date_input("新任务计划日期", value=datetime.now(ZoneInfo("Asia/Shanghai")).date())
                plan_time = st.time_input("新任务计划时间", value=time(23, 59), step=60)
                added = st.form_submit_button("添加本地任务")
            if added:
                try:
                    task_store.add(ns, title, course, local_plan(plan_date, plan_time) if use_plan else None, notes)
                    st.rerun()
                except Exception as exc:
                    error_message(exc)

elif page == "提醒中心":
    st.title("DDL 提醒中心")
    st.caption("本地后台服务独立于浏览器与 Canvas 登录。关机和休眠期间不能保证提醒，恢复运行后合并错过的节点。")
    state = service_status(home)
    st.info(f'服务：{"运行中" if state["running"] else "已停止"}；心跳：{"正常" if state["healthy"] else "未确认 / 超时"}；上次检查：{date_text(state.get("last_check")) if state.get("last_check") else "尚未检查"}')
    if state.get("error"):
        st.warning(state["error"])
    with st.container(horizontal=True):
        if st.button("启动提醒服务", disabled=st.session_state.demo or state["running"]):
            st.info(start_service(home))
        if st.button("停止提醒服务", disabled=st.session_state.demo or not state["running"]):
            st.info(stop_service(home))
        st.button("刷新提醒服务状态")
        if st.button("发送测试通知", disabled=st.session_state.demo):
            result = MacNotifier().send("Canvas 本地 DDL 提醒测试", "这是一条本机测试通知，不包含登录信息。当前时间：" + date_text(datetime.now(timezone.utc).isoformat()))
            if result["error"]:
                st.warning(result["error"])
            else:
                st.success("系统通知调用成功。请手动确认是否看到横幅；调用成功不代表已展示。")
    with st.expander("通知权限和服务说明"):
        st.write("首次测试后，请在 macOS 系统设置 → 通知中检查脚本通知相关应用的权限，并检查专注模式。测试按钮不会修改系统权限。Windows 本次只提供界面提醒中心。")
        st.write("关闭浏览器、Streamlit 或启动终端后，已启动的提醒进程继续工作；手动停止或进程异常退出后，需要重新启动。电脑重启后不会自启动，本次没有安装登录项。")
        st.caption(f"服务私有目录：{home}")
    if not ns:
        st.info("配置账户后可设置提醒。未登录也能处理之前已保存的任务，但需要已有账户配置。")
    else:
        preferences = reminder_settings(store, ns)
        with st.form("reminder_preferences"):
            enabled = st.checkbox("启用 DDL 提醒", value=preferences["enabled"])
            offsets = preferences["offsets"]
            first = st.checkbox("启用第一个节点", value=len(offsets) >= 1)
            hours1 = st.number_input("第一个节点：提前小时", min_value=0.0, max_value=8760.0, value=float(offsets[0]/60) if offsets else 24., step=0.25)
            second = st.checkbox("启用第二个节点", value=len(offsets) >= 2)
            hours2 = st.number_input("第二个节点：提前小时", min_value=0.0, max_value=8760.0, value=float(offsets[1]/60) if len(offsets) > 1 else 2., step=0.25)
            saved = st.form_submit_button("保存提醒设置")
        if saved:
            try:
                values = ([round(hours1*60)] if first else []) + ([round(hours2*60)] if second else [])
                set_settings(store, ns, enabled, values)
                st.success("提醒设置已保存；只重新安排未来节点，重复时间自动合并。")
            except Exception as exc:
                error_message(exc)
        all_events = st.checkbox("显示待发送、取消和已过期的节点", key="reminder_all")
        events = center(store, ns, all_events)
        names = {"pending": "待发送", "sending": "发送中 / 待核对", "sent": "通知调用已完成", "failed": "系统通知失败", "unknown": "上次结果未知，不自动重发", "expired": "首次获取时节点已过，不补发", "cancelled": "已取消"}
        if events:
            rows = []
            for e in events:
                snapshot = json.loads(e["snapshot"])
                rows.append({"课程": snapshot["course"], "任务": snapshot["title"], "日期类型": snapshot["date_source"], "截止/计划时间": date_text(snapshot["due"]),
                    "提醒节点": f'提前 {e["offset"]/60:g} 小时', "状态": names[e["status"]], "已读": bool(e["seen"]), "原因": e["error"]})
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", alt="本地 DDL 提醒记录和通知失败原因")
            if st.button("将提醒标为已读"):
                mark_seen(store, ns)
                st.rerun()
        else:
            st.info("尚无已触发的提醒。可以显示全部节点查看安排；新获取时已过去的节点不会补发。")

elif page == "资料库":
    st.title("本地资料库")
    st.caption("此页面读取本地 SQLite 和文件，打开界面不请求 Canvas。")
    resource_view(resources)

elif page == "作业要求":
    st.title("作业与截止时间")
    st.caption("截止时间按当前登录学生显示；未设置时不推测其他班级的日期。")
    resource_view([r for r in resources if r["kind"] == "assignment"], assignments=True)

elif page == "同步记录":
    st.title("同步记录与缺失原因")
    if last_run:
        st.caption(f'开始：{date_text(last_run["started"])}；结束：{date_text(last_run["ended"])}')
        if not last_run["ended"]:
            st.warning("上次同步未正常结束。可再次同步，完整缓存会被复用。")
        render_results(last_run["result"])
    source_table()
    failures = [r for r in resources if r.get("status") in {"failed", "denied"}]
    incomplete_ids = {s["course"] for s in store.sources(ns) if s["status"] != "success"} if ns else set()
    retry_ids = incomplete_ids | {r["course_id"] for r in failures}
    if failures:
        st.dataframe(pd.DataFrame([{"课程": course_map.get(r["course_id"], {}).get("name"), "资源": r.get("title"), "原因": r.get("reason")} for r in failures]), hide_index=True, width="stretch")
    if st.button("重试失败项目与未完成来源", disabled=not retry_ids):
        run_sync([c for c in courses if c["id"] in retry_ids])
    st.caption("重试会重新扫描存在失败的课程；已完整下载且未变化的文件跳过，权限未改变的资源仍显示无权限。")
