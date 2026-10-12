# 验证结果

验证日期：**2026-10-12（Asia/Shanghai）**。环境：macOS Apple Silicon、Python **3.14.2**、Streamlit **1.65.0**、httpx **0.28.1**、Playwright **1.63.0**、pytest **9.1.1**。

## 自动化验证

最终运行命令：

```bash
.venv/bin/python -m pytest --junitxml=/private/tmp/canvas-sync-test-results.xml
.venv/bin/python -m compileall -q app.py canvas_sync tests
bash -n start.command install-browser.command
.venv/bin/python -m pip check
```

结果：**68 passed**；Python 编译检查、macOS 启动脚本语法检查及依赖一致性检查通过。测试使用临时 SQLite、临时资料目录和模拟 HTTP 响应，不接触真实账号。

| 场景 | 验证内容 | 结果 |
| --- | --- | --- |
| 课程分页 | 多页当前与历史课程、同名课程以 ID 区分、Link 参数不重新拼接 | 通过 |
| 文件与模块分页 | 多页 Files，内嵌 items 被截断后继续完整分页读取 | 通过 |
| 缓存去重 | 同一附件从 Files、作业、模块和不同课程引用，网络只下载一次 | 通过 |
| 安全目录 | 中文、Windows 保留名称、控制字符、超长组件、路径穿越、符号链接、大小写与同名冲突、已有个人文件 | 通过 |
| 临时下载安全 | 预先存在的可预测 `.part` 符号链接不被跟随 | 通过 |
| 增量同步 | 再次跳过 9 个未变化资源、网络下载为 0、远程更新保留旧副本 | 通过 |
| 缺失/损坏 | 丢失副本从缓存恢复、损坏缓存重新网络下载与哈希校验 | 通过 |
| 本地修改 | 工作 notebook 原样保留、远程另存、整组远程 notebook/CSV 保持关系 | 通过 |
| notebook 关系 | 作业同目录 CSV，以及原有 `data/data.csv` 子目录均保持 | 通过 |
| 无附件作业 | 保存可离线查看的题目、表格、结构、Canvas 图片及原页面链接 | 通过 |
| 学生截止时间 | 使用当前用户 due_at，忽略其他班级 all_dates；Shanghai 时区转换 | 通过 |
| 无权限/部分失败 | Files 无权限后继续作业/页面/模块、分页第二页失败保留已获取结果、部分状态可见 | 通过 |
| 登录失效 | 401、登录 HTML、SSO 跳转、无有效 JSON 不误当附件保存 | 通过 |
| 限流/中断 | 429 Retry-After、503 恢复、有限重试、读取流中断后重新下载、临时文件清理 | 通过 |
| 认证隔离 | 跨域下载不携带 Authorization/Cookie；跨域分页拒绝读取；Cookie 父域与路径匹配 | 通过 |
| 凭据保护 | 配置不含 Token、仅系统安全钥匙串后端、导出与 SQLite 不含签名下载地址 | 通过 |
| 浏览器登录组件 | 手动登录生命周期、真实 JSON 结构验证、同源 Cookie 导出、独立配置、不保存密码（组件模拟） | 通过 |
| 不越权 | 锁定模块不请求项目/进度；锁定项目解锁后错误状态恢复；网络层只 GET | 通过 |
| 异常响应 | 非标准列表条目显示部分获取，缺失正文不覆盖既有离线要求 | 通过 |
| 同步互斥 | 同一资料库的并行同步被拒绝，退出后锁释放 | 通过 |
| 脚本不执行 | py、ipynb、command、ps1、exe、pl、js、sh 只在文件管理器定位 | 通过 |
| 界面流程 | 首次无配置提示、演示模式、课程刷新、全选三门课程、同步、资料库、作业、同步记录、错误配置验证 | 通过 |

## 本机运行验证

- 真实 Streamlit 服务在 `127.0.0.1:8501` 启动，中文界面在浏览器中可访问。
- 浏览器中完成演示课程刷新、全选、多门课程同步，显示 27 项模拟资源；后续单课程同步显示 9 项跳过、0 网络下载、0 资源失败、0 未完成来源。
- 离线作业要求中可以查看当前账户截止时间、列表、表格、Canvas 附件和未下载外部链接。
- 用 **已安装的 Microsoft Edge** 启动 Playwright 临时独立配置，加载本地测试 HTML并验证标题内容，随后关闭，成功。未访问真实学校。
- 官方 Chromium 的 CDN 下载在当前开发网络中返回连接重置，未成功安装。登录组件会尝试已安装的 Chrome/Edge，README 提供 Chromium 安装脚本与步骤。

界面截图为模拟数据，保存在 [docs/demo-ui.jpg](docs/demo-ui.jpg)。

## 尚未验证

没有真实 Canvas 账号、学校 Token 或浏览器登录信息：学校 SSO/MFA、个人 Token 是否开放、会话调用 API 的具体策略、实际课程权限和文件下载域名、真实学生截止时间差异尚未联调。

Windows 分支尚未实机运行。通用页面解析、Canvas Block Editor 完整渲染、外部登录平台下载、HTTP Range 续传和远程删除对账未实现。详细使用步骤及限制见 [README.md](README.md)。
