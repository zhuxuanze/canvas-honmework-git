# Canvas 课程资料同步器

一个在自己电脑运行的中文个人资料库。启动先显示本地缓存；按需读取 Canvas 官方 REST API，下载作业附件、课程文件和页面，保留旧版本与本地修改。只监听 `127.0.0.1`，无需服务器、付费服务或多用户账户。

**真实学校账号尚未联调。** 演示数据、自动化测试和本地界面已验证；学校 SSO、Token 权限和具体课程响应需要你在本机登录验证。不要把 Token、Cookie 或会话文件发给开发者。

## 1. 安装与启动

需要 Python **3.11 或更新版本**；开发验证环境为 macOS Apple Silicon、Python 3.14.2。下载 Python 时可使用 [Python 官方安装程序](https://www.python.org/downloads/)。Windows 安装时勾选 **Add Python to PATH**。

### macOS

在 Finder 中双击 `start.command`。首次启动会创建项目自己的 `.venv` 并安装依赖，需要联网；以后会复用环境。若脚本没有执行权限，在终端进入项目后运行：

```bash
cd /Users/zhuxuanze/Documents/ChatGPT/canvas-homework-git
chmod +x start.command install-browser.command
./start.command
```

启动后访问 [http://127.0.0.1:8501](http://127.0.0.1:8501)。保持终端窗口运行；关闭时在终端按 **Control+C**。

### Windows

将完整项目放到一个目录（建议 `C:\CanvasSync`），双击 `start.bat`，然后访问同一个本机地址。Python 需要支持 `py -3`；若没有 Python Launcher，先用以下命令创建环境：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.showEmailPrompt false --browser.gatherUsageStats false
```

Windows 启动、钥匙串和文件管理器分支已实现，但未在真实 Windows 电脑验证。

### 不想先登录：体验演示

侧栏点击 **体验演示数据** → **联网刷新课程列表** → **全选当前结果** → **同步选中课程**。

界面始终标记“演示数据”；模拟账户、课程、截止时间和文件均不是学校内容。使用本地 MockTransport，不向模拟域名发出真实请求。演示包含 PDF、文本、图片、notebook、CSV、无附件作业、页面、截断的模块项目以及历史课程。演示资料独立保存到应用私有目录下的 `演示资料`。

## 2. 配置 Canvas 与登录

进入侧栏 **连接配置**：

1. 填学校 Canvas 根地址，例如 `https://canvas.example.edu`，不填写 `/courses/...`、SSO 地址或任何查询参数。只接受 HTTPS，不关闭证书验证。
2. 填资料保存目录，使用绝对路径。默认 `~/Downloads/Canvas资料`。Windows 建议用较短路径，例如 `C:\Canvas资料`。
3. 选择以下任一认证方式，点击 **测试连接并保存配置**。成功会显示当前账户姓名和稳定用户 ID。之后进入 **课程与同步**。

### 学校允许个人 Access Token

在 Canvas **账户 → 设置 → 已批准的集成 → 新建访问令牌** 中生成令牌；如果学校没有此入口或不允许生成，不使用此方式。

将 Token 仅输入本地界面的密码框。可以选择：

- **仅本次使用**：留在当前浏览器会话的内存中，刷新页面或重启后可能需要重新输入；不写入配置或 SQLite。
- **系统钥匙串**：使用 macOS Keychain / Windows Credential Manager（Linux 支持可用的 Secret Service）。系统安全后端不可用时提示改用本次使用，不回退到明文文件。已有钥匙串 Token 可在输入框留空时读取。需要撤销时到 Canvas 令牌管理页撤销，并在系统钥匙串中删除 `CanvasLocalSync` 项目。

Token 始终放入同域名请求的 Authorization 头，不放在 URL、日志或导出资料中。

### 学校没有开放 Token：打开浏览器登录

1. 选择 **打开浏览器登录**，点击同名按钮。
2. 工具尝试 Playwright Chromium，再尝试已安装的 Google Chrome / Microsoft Edge。每种方式都使用工具专用的浏览器配置目录，和日常浏览器的配置分开；关闭保存密码功能。
3. **由你手动完成**学校 SSO、验证码和二次验证。工具不填写、读取或保存密码。
4. 工具用该浏览器会话 GET `/api/v1/users/self/profile` 验证账户 JSON，最长等待 15 分钟；通过后保存适用于 Canvas 域名的 Cookie 并关闭登录窗口。
5. 回到界面点 **刷新登录状态**，再点 **测试连接并保存配置**，复验 HTTP 客户端能否用该会话读账户接口。

如果没有可用的 Chrome/Edge，macOS 双击 `install-browser.command`，Windows 双击 `install-browser.bat`，下载官方 Playwright Chromium 后重试。当前开发网络下载 Chromium CDN 失败，但已加入已安装浏览器的替代路径。安装失败时请检查网络/学校代理；不要关闭 TLS 验证。

**浏览器能登录不一定代表会话 API 可用。** API 返回登录页、401、外部 SSO 跳转或非 JSON 时，会明确提示重新登录或学校不支持此接口。首版没有通用页面抓取：API 被学校禁用、Block Editor 或特殊自定义页面需要在获得真实已授权响应后另行适配，不会猜测学校的 HTML 或绕过权限。

## 3. 选择课程与同步

1. 点击 **联网刷新课程列表**。分别完整读取当前 `active` 与历史 `completed` 课程，跟随每一页 Link；显示名称、学期、范围和课程 ID。无权限访问的历史课程不会被强行读取。
2. 搜索名称、学期或 ID，按当前/历史课程过滤。在表格的“同步”列勾选多门课程；可以 **全选当前结果**。搜索不会取消其他已选择课程。
3. 点击 **记住课程选择** 或直接 **同步选中课程**，选择会保存。也可在下方选课程，点击 **同步这门课程**。
4. Canvas 未提供学期时，展开 **设置此课程的本地学期** 手动填写。改学期或课程更名后下次写入新目录，旧目录保留。
5. 同步过程显示当前课程、正在读取的入口和附件进度。默认最多 3 个并发下载；同一资料库的第二次并行同步会被阻止。
6. 看 **同步记录**：不仅有新增/更新/跳过/资源失败/无权限/本地修改冲突数量，还显示 Files、Folders、Assignments、Pages、Modules 及模块项目明细。

“列表完整获取”仅表示该入口的枚举完成；具体附件/正文保存结果在资源状态中。出现分页失败、权限拒绝、锁定模块或下载失败时，界面不会显示全部获取成功。

**重试失败项目与未完成来源** 会重扫存在失败的课程，复用完整缓存并跳过未变化文件。权限没有变化的项目仍会显示原因。断网或关闭程序后再次同步会处理未完成的项目；首版不提供单个文件的 HTTP Range 续传，未完成文件会重新流式下载。

## 4. 查看资料与作业

侧栏 **资料库** 可以按课程、类型、名称搜索，查看状态、来源、获取时间、远程更新时间与本地保存时间。**作业要求** 中有截止时间和离线正文预览；表格、题目结构和可访问的 Canvas 图片会保存。

- **打开本地文件**：用系统应用查看常见文档。脚本、notebook 和未知文件类型仅在文件管理器定位，不执行。
- **打开所在文件夹**：查看作业的 notebook、CSV 等配套文件。
- **打开 Canvas 原页面**：联网查看最新页面/学校特殊内容。
- 每个课程有 **课程索引.html**，不运行本工具也能在浏览器打开，查看本地链接、来源、截止时间、获取情况和失败原因。

截止时间直接使用 Canvas 对**当前请求用户**返回的 `due_at`，默认转成 **Asia/Shanghai**。不会选择 `all_dates` 里的其他班级日期；缺失/空值显示“未设置截止时间”。请结合内容获取时间判断缓存是否新鲜。

## 5. 目录、分类与本地修改

```text
资料保存目录/
  Canvas-实例与用户标识/
    2026 秋季/
      Python 与数据科学__课程123/
        作业/
          作业一__456/
            _Canvas要求.html
            analysis.ipynb
            data.csv
        课程资料/
          Files/原文件夹/...
          Modules/第一周__模块789/...
        课程页面/学习指南__页面ID/_Canvas要求.html
        其他资料/...
        课程索引.html
  .canvas-sync/实例与用户标识/cache/文件ID/版本/
```

实例与用户前缀防止不同账号或学校混在一起；前缀下按 **学期 → 课程 → 内容** 排列。课程、作业、模块的 ID 为稳定标识，同名课程不会合并。

分类依据原 Files 文件夹、模块引用、作业/页面的真实 Canvas 附件引用，不凭文件名猜作业。附件下载缓存按 Canvas 文件 ID 和远程版本去重，课程、作业和模块之间使用独立副本，避免编辑一处影响其他位置。

一组作业/页面/模块附件沿原文件夹的共同父目录保留相对目录：同一原文件夹的 `analysis.ipynb` 和 `data.csv` 保持同目录；原 `data/data.csv` 会保留子目录。缺少文件夹权限时只能按可用关联放在同目录，**不会分析或执行 notebook 来猜依赖**。没有被任何 API/正文/模块引用的数据文件、外部数据源和写死的绝对路径不能保证自动修复。

名称会处理中文、控制字符、Windows 保留名称、斜杠、路径穿越及过长组件。同名/大小写冲突加稳定后缀，已有非工具文件不被覆盖；目录中的符号链接拒绝写入。非法名称、重名或超长名称导致文件名变化时，notebook 里写死的文件名可能需要你手动调整。

增量策略：

- 远程更新时间/大小/文件名未变化，且缓存 SHA-256/长度完整：不再下载网络文件。
- 没有可靠更新时间：保守重新获取。
- 本地资料副本丢失：从完整缓存恢复；缓存丢失/损坏：重新下载。
- 已管理的副本内容与上次保存哈希不同：视为**本地修改**，即使可能是损坏也不静默覆盖。无法可靠区分用户编辑与损坏；远程版本另存为 `~Canvas远程-...`。
- 正常远程更新：将原副本保留在 `_Canvas历史版本`，再原子保存新内容。
- 作业/模块附件冲突：保留工作目录里的修改，并在 `_Canvas远程组合/版本/` 中保存整组远程附件及要求，维持 notebook/CSV 的配套路径。远程组合也不覆盖你后续的修改。
- 默认**不删除**本地资料。课程/资源远程消失时，本地旧资料可能仍出现在索引；界面展示获取时间，首版没有远程删除对账。

## 6. 私有数据位置与备份

应用配置、SQLite、Cookie 和独立浏览器目录位于：

| 系统 | 应用目录 |
| --- | --- |
| macOS | `~/Library/Application Support/CanvasLocalSync` |
| Windows | `%LOCALAPPDATA%\CanvasLocalSync` |
| Linux | `~/.local/share/canvas-local-sync` |

可用环境变量 `CANVAS_SYNC_HOME` 改目录（测试使用临时目录）。macOS/Linux 目录为 `0700`，配置和 Cookie 文件为 `0600`；Windows 依赖当前用户目录的系统 ACL。Cookie 和浏览器配置包含认证会话，按凭据保管；不要把应用私有目录同步到公开云盘。

`config.json` 只记录地址、输出目录、认证方式和账户标识；Token 保存在系统钥匙串或当前会话内存中。SQLite/资料索引不保存 Token、Cookie 或带签名的下载地址。资料中的认证/签名查询参数在导出链接时去除。`.gitignore` 排除了运行环境、会话、浏览器配置、数据库和缓存；不要手动把个人运行数据加入仓库。

备份时可以复制资料目录；如需保留同步清单，退出程序后备份 `state.sqlite3` 和配置。**不要把 `sessions` 作为普通资料导出。** 在个人电脑上运行，不开公网端口。

## 7. 测试与开发

```bash
# macOS/Linux，项目目录内
.venv/bin/python -m pip install 'pytest>=8,<10'
.venv/bin/python -m pytest
```

```powershell
# Windows
.venv\Scripts\python.exe -m pip install "pytest>=8,<10"
.venv\Scripts\python.exe -m pytest
```

测试使用模拟 HTTP 响应和 Streamlit AppTest，不需要真实账号。具体结果见 [TEST_RESULTS.md](TEST_RESULTS.md)。核心验证覆盖分页、模块截断、去重、同名文件、安全路径、增量更新、哈希校验、本地修改保护、notebook 相对目录、权限与登录失效、限流、网络中断、课程选择和界面完整演示流程。

代码分层：

| 文件 | 职责 |
| --- | --- |
| `app.py` | 中文界面、本地查询、配置与同步操作 |
| `canvas_sync/config.py`、`browser_login.py` | 配置、系统钥匙串、独立浏览器手动登录 |
| `canvas_sync/http.py` | GET、分页、有限重试、跨域认证隔离、流式下载与验证 |
| `canvas_sync/canvas.py` | Canvas 官方 API 适配 |
| `canvas_sync/engine.py` | 来源枚举、关联分类、同步、课程索引 |
| `canvas_sync/materialize.py`、`paths.py`、`lock.py` | 副本/历史/冲突、路径安全、同步互斥 |
| `canvas_sync/store.py` | SQLite 本地缓存与同步状态 |
| `canvas_sync/demo.py` | 明确标注的模拟课程与 HTTP 响应 |

未实现 Unival。将来可添加平台适配层；目前不引入其他平台依赖，也不包含 AI 分类、作业提交、课程编辑、学习进度提交、定时任务或通知。

## 8. 真实账号验证清单与限制

请你在本机完成以下验证，不向聊天发送登录凭据：

1. 配置学校根地址并验证账户 ID；浏览器方式确认“会话可读取 API”。
2. 刷新课程，确认有期望的当前/历史课程和正确学期。
3. 先同步一门小课程，对比 Files、Assignments、Pages、Modules 的入口状态、资源数量和课程网页。查看同步记录里的部分失败原因。
4. 打开一个无附件作业和一个带图片/表格的作业，对比要求与当前学生的截止时间。
5. 打开带 notebook/CSV 的作业文件夹，确认其数据引用所需的相对位置（由你自主执行 notebook，本工具不执行）。
6. 再同步一次确认未变化文件跳过。修改一个本地工作副本，再同步确认其内容保留并生成远程副本/完整组合。
7. 会话过期后重新登录，按“重试失败项目与未完成来源”恢复。

实际限制：

- 没有真实 Canvas 账户验证，学校 SSO、Cookie 策略、API 权限、已隐藏课程和下载域名的实际行为尚未确认。
- 浏览器会话 API 可能被学校禁止；无 Token 且 API 不可用时需要根据真实已授权页面单独开发解析器。首版不以通用猜测代替适配。
- HTML 正文保留可访问的 Canvas 图片/附件；外部图片、视频、网盘、ExternalTool、测验、讨论及需要另外登录的服务留链接与说明，标记未下载。Block Editor 页面标记部分获取。
- 导出 HTML 移除脚本、主动媒体和远程图片加载；不保证完全复刻 Canvas 的样式、公式插件或交互组件。原页面链接保留。
- 下载流式写临时文件并检查长度、HTML 登录页、notebook JSON；不对所有文件类型做格式内部校验或病毒扫描。限流/临时错误有有限退避，最多等待 30 秒每次；不做无限重试。
- 全程 GET，明确不调用提交/修改/mark_read 接口。不过具体学校服务器可能把读取行为写入访问日志；需要在真实联调中确认其定制行为。
- 默认验证 TLS。校园自签名证书、必须使用特殊代理或额外客户端证书的环境需要按学校策略提供适配，当前不会关闭证书验证；HTTP 客户端不自动采用系统代理环境变量。
- Windows 路径总长度、系统文件关联、凭据 ACL 与中文启动脚本仍需 Windows 实机验证。建议使用短保存根目录；超长路径失败会记录原因并继续其他内容。

## 9. 开发前核对的 Canvas 官方文档

实现依据 Instructure 官方文档，不调用写入接口：

- [API 分页](https://developerdocs.instructure.com/services/canvas/basics/file.pagination)：跟随不透明的 Link 头，不依赖 `per_page` 就拿到全部内容。
- [Courses](https://developerdocs.instructure.com/services/canvas/resources/courses)：课程枚举、学期、active/completed。
- [Assignments](https://developerdocs.instructure.com/services/canvas/resources/assignments)：HTML 正文、当前请求用户适用的 `due_at`、锁定信息。
- [Modules](https://developerdocs.instructure.com/services/canvas/resources/modules)：内嵌 items 可能省略，必要时继续请求分页项目接口。
- [Files](https://developerdocs.instructure.com/services/canvas/resources/files)：元数据、文件夹、下载 URL 和权限。
- [Pages](https://developerdocs.instructure.com/services/canvas/resources/pages)：分页列表与页面详情正文。
- [限流](https://developerdocs.instructure.com/services/canvas/basics/file.throttling)：429 与适度并发。
- [官方 OAuth2 与个人 Token 说明](https://canvas.instructure.com/doc/api/file.oauth.html)。
