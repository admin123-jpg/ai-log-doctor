# CI/CD 流水线常见错误排查手册

## pip install 依赖安装失败

**现象**：流水线在 `pip install -r requirements.txt` 步骤失败，出现
`ERROR: Could not find a version that satisfies the requirement xxx`
或 `No matching distribution found for xxx`。

**常见原因**：
1. 依赖包需要编译（如 chromadb 依赖的 onnxruntime），当前 Python 版本没有对应 wheel。
2. Python 版本和包要求的版本不兼容。
3. 网络问题导致下载超时。
4. 包名拼错或该包已下架。

**排查步骤**：
- 在本地用同样的 Python 版本复现：`python -m pip install -r requirements.txt`
- 看报错里提到的具体包名，去 PyPI 查它支持的 Python 版本
- 检查 `python-version` 配置是否过高或过低

**解决方案**：
- 固定 Python 版本，例如 `python-version: '3.11'`。
- 给编译型依赖加超时和重试：`pip install --timeout 120 --retries 3 -r requirements.txt`
- 换用纯 Python 实现的替代方案（例如向量库不装 chromadb，改用手写的内存检索）。
- 配置国内镜像源加速：`pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple`

## 单元测试失败导致流水线中断

**现象**：`pytest` 步骤报红，出现 `FAILED tests/test_xxx.py::test_yyy - AssertionError`，退出码非 0。

**常见原因**：
1. 代码改动引入了 bug。
2. 测试依赖了本地环境（数据库、文件路径），CI 环境里不存在。
3. 测试用例本身写得不稳健（依赖执行顺序、时间、随机数）。

**排查步骤**：
- 点开 GitHub Actions 的失败步骤，看具体哪个断言失败
- 本地运行同样的命令复现：`pytest -v`
- 检查 CI 环境是否缺少环境变量

**解决方案**：
- 修复代码 bug 或修正测试断言。
- 在 workflow 里配置测试所需的环境变量（通过 GitHub Secrets）。
- 用 `pytest -p no:randomly` 或显式设置随机种子，保证测试可重复。

## Docker 镜像构建失败

**现象**：`docker build` 步骤失败，出现 `COPY failed: file not found in build context` 或 `failed to solve`。

**常见原因**：
1. `.dockerignore` 把需要的文件排除了。
2. Dockerfile 里 COPY 的路径写错（路径是相对于构建上下文的）。
3. 构建上下文目录不对。

**排查步骤**：
- 检查 Dockerfile 里 COPY 的源文件是否真实存在
- 查看 `.dockerignore` 是否误伤
- 本地执行同样的 `docker build -t test .` 复现

**解决方案**：
- 修正 COPY 路径，注意是相对路径（用 `.` 开头，如 `COPY ./src /app/src`）。
- 调整 `.dockerignore`，只排除真正不需要的（`__pycache__`、`.git`、`venv`、`.env`）。

## 镜像推送被拒绝 denied

**现象**：`docker push` 失败，出现 `denied: requested access to the resource is denied` 或 `unauthorized`。

**常见原因**：
1. 没有登录镜像仓库，或登录信息过期。
2. 仓库名/命名空间写错。
3. 使用的 Token 权限不足（缺 write:packages）。

**解决方案**：
- 先在 workflow 里用 `docker/login-action` 登录。
- 用 GitHub 自动提供的 `secrets.GITHUB_TOKEN`，并在 job 里声明 `permissions: packages: write`。
- 镜像名要带命名空间，例如 `ghcr.io/<用户名>/<仓库名>:latest`。

## 部署后服务健康检查失败

**现象**：部署步骤卡住直到超时，日志显示容器反复重启，健康检查不通过。

**常见原因**：
1. 应用启动慢，健康检查的超时时间太短。
2. 应用启动报错（配置缺失、端口冲突、依赖服务不可达）。
3. 健康检查探针的路径不存在。

**排查步骤**：
- `docker logs <容器名>` 看应用启动日志
- 手动 curl 健康检查地址确认返回码

**解决方案**：
- 给应用加 `/health` 健康检查接口，只做轻量检查（不查数据库）。
- 调大 `start_period` 和 `interval`，给慢启动应用留足时间。
- 检查部署环境的环境变量是否齐全。

## 流水线超时被取消

**现象**：Job 运行超过 6 小时（或自托管 runner 超时）被自动取消，日志出现 `The job running on runner ... has exceeded the maximum execution time`。

**常见原因**：
1. 某个步骤卡死（比如下载一直等待、命令等待用户输入）。
2. 构建步骤没有缓存，每次全量编译很慢。

**解决方案**：
- 给 job 设置 `timeout-minutes: 30` 快速失败，避免长时间占用。
- 配置依赖缓存：`actions/setup-python` 加 `cache: 'pip'`。
- 检查是否有命令在等待交互输入，必要时加 `-y` 或 `< /dev/null`。
