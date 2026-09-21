# Python / Flask 应用常见错误排查手册

## KeyError 字典键不存在

**现象**：`KeyError: 'user_id'`，接口返回 500。

**常见原因**：
1. 请求参数里没有传这个字段，但代码直接用了 `data['user_id']`。
2. 上游返回的 JSON 结构和预期不一致。
3. 拼写错误（大小写、下划线）。

**解决方案**：
- 用 `data.get('user_id')` 代替 `data['user_id']`，取不到返回 None 而不是抛异常。
- 在接口入口处做参数校验（Flask 可以用 Pydantic 或 marshmallow）。
- 打印原始请求体确认结构：`print(request.json)`

## requests ReadTimeout 请求超时

**现象**：`requests.exceptions.ReadTimeout: HTTPConnectionPool(host='xxx', port=80): Read timed out. (read timeout=30)`

**常见原因**：
1. 被调方处理太慢。
2. 没有设置超时时间，一直等待（默认 requests 不设超时会无限等待）。
3. 网络抖动或被调方不可达。

**解决方案**：
- 所有外部调用都必须显式设置超时：`requests.post(url, json=payload, timeout=(5, 30))`（连接 5 秒、读取 30 秒）。
- 加重试机制，用 `urllib3.Retry` 或 `tenacity` 库。
- 耗时任务改成异步（Celery / 消息队列），不要阻塞请求线程。

## ModuleNotFoundError 模块找不到

**现象**：`ModuleNotFoundError: No module named 'flask'`。

**常见原因**：
1. 依赖没装，或装到了别的 Python 环境 / 别的虚拟环境。
2. Docker 镜像里没有执行 `pip install -r requirements.txt`。
3. 本地开发用了系统 Python，容器里用的是另一个版本。

**排查步骤**：
- `which python`（Linux/Mac）或 `where python`（Windows）确认当前用的解释器
- `pip list | grep flask` 确认包装上了没
- VS Code 右下角确认选中的解释器是否是虚拟环境的那个

**解决方案**：
- 激活虚拟环境后再安装：`source venv/bin/activate`（Windows 用 `venv\Scripts\activate`）
- 在 Dockerfile 里确保 `COPY requirements.txt .` 在 `RUN pip install` 之前，且顺序正确。

## 数据库连接未关闭导致泄漏

**现象**：运行一段时间后报 `Too many connections`，或应用越来越慢。

**常见原因**：
1. 拿到连接后没有关闭，尤其在异常分支里漏了 `close()`。
2. 每次请求新建连接，没有用连接池。

**解决方案**：
- 用 `try ... finally` 确保连接被关闭。
- 更好的方式是用上下文管理器：`with conn.cursor() as cur: ...`
- 生产环境用连接池（如 SQLAlchemy 的 pool、DBUtils）。

## 500 Internal Server Error 但日志看不到堆栈

**现象**：接口返回 500，但容器日志里没有 Traceback。

**常见原因**：
1. 日志级别设置成了 WARNING 以上，ERROR 没打印出来。
2. 异常被 `try...except: pass` 吞掉了。
3. Flask 处于生产模式，错误没有回显。

**解决方案**：
- 配置日志级别为 INFO 或 DEBUG，并输出到 stdout（容器里 stdout 才能被 docker logs 收集）。
- 不要用裸 `except: pass`，至少 `except Exception as e: logger.exception(e)`。
- 全局异常处理：Flask 注册 `@app.errorhandler(Exception)`。

## 中文日志乱码

**现象**：日志里的中文显示为 `???` 或 `\u4e2d\u6587`。

**常见原因**：
1. 文件读写没有指定 UTF-8 编码。
2. 写入 JSON 时用了 `ensure_ascii=True`（默认值），中文被转义成 Unicode。
3. 终端/容器环境的 LANG 不是 UTF-8。

**解决方案**：
- 所有 `open()` 都显式指定 `encoding="utf-8"`。
- `json.dumps()` 加参数 `ensure_ascii=False`。
- Dockerfile 里设置环境变量 `ENV PYTHONIOENCODING=utf-8` 和 `ENV LANG=C.UTF-8`。
