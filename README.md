# OpenAI 账号管理系统 v2

管理 OpenAI 账号的 Web UI 系统，支持多种邮箱服务、并发批量注册、代理管理和账号管理。

# 官方拉闸了,改变了授权流程,各位自行研究吧  

> ⚠️ **免责声明**：本工具仅供学习和研究使用，使用本工具产生的一切后果由使用者自行承担。请遵守相关服务的使用条款，不要用于任何违法或不当用途。 如有侵权，请及时联系，会及时删除。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

## 功能特性

- **多邮箱服务支持**
  - Tempmail.lol（临时邮箱，无需配置）
  - Outlook（IMAP + XOAUTH2，支持批量导入）
  - 自定义域名（两种子类型）
    - **MoeMail**：标准 REST API，配置 API 地址 + API 密钥
    - **TempMail**：自部署 Cloudflare Worker 临时邮箱，配置 Worker 地址 + Admin 密码
  - DuckMail
    - **DuckMail API**：兼容 DuckMail 接口，手动填写 API 地址、默认域名，可选 API Key

- **注册模式**
  - 单次注册
  - 批量注册（可配置数量和间隔时间）
  - Outlook 批量注册（指定账户逐一注册）

- **并发控制**
  - 流水线模式（Pipeline）：每隔 interval 秒启动新任务，限制最大并发数
  - 并行模式（Parallel）：所有任务同时提交，Semaphore 控制最大并发
  - 并发数可在 UI 自定义（1-50）
  - 日志混合显示，带 `[任务N]` 前缀区分

- **实时监控**
  - WebSocket 实时日志推送
  - 跨页面导航后自动重连
  - 降级轮询备用方案

- **代理管理**
  - 动态代理（通过 API 每次获取新 IP）
  - 代理列表（随机选取，支持设置默认代理，记录使用时间）

- **账号管理**
  - 查看、删除、批量操作
  - Token 刷新与验证
  - 订阅状态管理（手动标记 / 自动检测 plus/team/free）
  - 导出格式：JSON / CSV / CPA 格式 / Sub2API 格式
    - 单个账号导出为独立 `.json` 文件
    - 多个 CPA 账号打包为 `.zip`，每个账号一个独立文件
    - Sub2API 格式所有账号合并为单个 JSON
  - 上传目标（直连不走代理）：
    - **CPA**：支持多服务配置，上传时选择目标服务，可按服务开关将账号实际代理写入 auth file 的 `proxy_url`
    - **Sub2API**：支持多服务配置，标准 sub2api-data 格式
    - **Team Manager**：支持多服务配置

- **支付升级**
  - 为账号生成 ChatGPT Plus 或 Team 订阅支付链接
  - 后端命令行以无痕模式自动打开 Chrome/Edge
  - Team 套餐支持自定义工作区名称、座位数、计费周期

- **系统设置**
  - 代理配置（动态代理 + 代理列表，支持设默认）
  - CPA 服务列表管理（多服务，连接测试）
  - Sub2API 服务列表管理（多服务，连接测试）
  - Team Manager 服务列表管理（多服务，连接测试）
  - Outlook OAuth 参数
  - 注册参数（超时、重试、密码长度等）
  - 验证码等待配置
  - 数据库管理（备份、清理）
  - 支持远程 PostgreSQL

## 快速开始

### 环境要求

- Python 3.10+
- [uv](https://github.com/astral-sh/uv)（推荐）或 pip

### 安装依赖

```bash
# 使用 uv（推荐）
uv sync

# 或使用 pip
pip install -r requirements.txt
```

### 环境变量配置（可选）

复制 `.env.example` 为 `.env`，按需填写：

```bash
cp .env.example .env
```

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `APP_HOST` | 监听主机 | `0.0.0.0` |
| `APP_PORT` | 监听端口 | `8000` |
| `APP_ACCESS_PASSWORD` | Web UI 访问密钥 | `admin123` |
| `APP_DATABASE_URL` | 数据库连接字符串 | `data/database.db` |

> 优先级：命令行参数 > 环境变量（`.env`）> 数据库设置 > 默认值

### 启动 Web UI

```bash
# 默认启动（127.0.0.1:8000）
python webui.py

# 指定地址和端口
python webui.py --host 0.0.0.0 --port 8080

# 调试模式（热重载）
python webui.py --debug

# 设置 Web UI 访问密钥
python webui.py --access-password mypassword

# 组合参数
python webui.py --host 0.0.0.0 --port 8080 --access-password mypassword
```

> `--access-password` 优先级高于数据库中保存的密钥设置，每次启动时生效。打包后的 exe 同样支持此参数：
> ```bash
> codex-register.exe --access-password mypassword
> ```

### Docker 部署

项目支持通过 Docker 进行容器化部署。Docker 镜像已托管至 GitHub Container Registry (GHCR)。

#### 使用 docker-compose (推荐)

在项目根目录下，直接使用 `docker-compose` 启动：

```bash
docker-compose up -d
```
你可以在 `docker-compose.yml` 中修改相关的环境变量，例如配置端口或者设置 `WEBUI_ACCESS_PASSWORD` 访问密码。

#### 直接使用 docker run

如果你不想使用 docker-compose，也可以直接拉取并运行镜像：

```bash
docker run -d \
  -p 15555:15555 \
  -e WEBUI_HOST=0.0.0.0 \
  -e WEBUI_PORT=15555 \
  -e WEBUI_ACCESS_PASSWORD=your_secure_password \
  -v $(pwd)/data:/app/data \
  --name codex-register \
  ghcr.io/yunxilyf/codex-register:latest
```

环境变量说明：
- `WEBUI_HOST`: 监听的主机地址 (默认 `0.0.0.0`)
- `WEBUI_PORT`: 监听的端口 (默认 `15555`)
- `WEBUI_ACCESS_PASSWORD`: 设置 Web UI 的访问密码
- `DEBUG`: 设为 `1` 或 `true` 开启调试模式
- `LOG_LEVEL`: 日志级别，如 `info`, `debug`

> **注意**：`-v $(pwd)/data:/app/data` 挂载参数非常重要，它确保了你的数据库文件和账户信息在容器重启或更新后不会丢失。

### 使用远程 PostgreSQL

通过环境变量指定数据库连接字符串：

```bash
export APP_DATABASE_URL="postgresql://user:password@host:5432/dbname"
python webui.py
```

也支持 `DATABASE_URL`，优先级低于 `APP_DATABASE_URL`。

启动后访问 http://127.0.0.1:8000

## 打包为可执行文件

```bash
# Windows
build.bat

# Linux/macOS
bash build.sh
```

打包后生成 `codex-register.exe`（Windows）或 `codex-register`（Unix），双击或直接运行即可，无需安装 Python 环境。

## 项目结构

```
codex-register-v2/
├── webui.py            # Web UI 入口
├── build.bat           # Windows 打包脚本
├── build.sh            # Linux/macOS 打包脚本
├── src/
│   ├── config/         # 配置管理（Pydantic Settings）
│   ├── core/
│   │   ├── openai/     # OAuth、Token 刷新、支付核心
│   │   └── upload/     # CPA / Sub2API / Team Manager 上传模块
│   ├── database/       # 数据库（SQLAlchemy + SQLite/PostgreSQL）
│   ├── services/       # 邮箱服务实现
│   └── web/
│       ├── app.py      # 应用入口、路由挂载
│       ├── task_manager.py  # 任务/日志/WebSocket 管理
│       └── routes/     # API 路由
│           └── upload/ # CPA / Sub2API / TM 服务管理路由
├── templates/          # Jinja2 HTML 模板
├── static/             # 静态资源（CSS / JS）
└── data/               # 运行时数据目录（数据库、日志）
```

## 技术栈

| 层级 | 技术 |
|------|------|
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | SQLAlchemy + SQLite / PostgreSQL |
| 模板引擎 | Jinja2 |
| HTTP 客户端 | curl_cffi（浏览器指纹模拟） |
| 实时通信 | WebSocket |
| 并发 | asyncio Semaphore + ThreadPoolExecutor |
| 前端 | 原生 JavaScript（无框架） |
| 打包 | PyInstaller |

## API 端点

### 注册任务

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/registration/start` | 启动注册任务 |
| GET | `/api/registration/tasks` | 任务列表 |
| GET | `/api/registration/tasks/{uuid}/logs` | 任务日志 |
| POST | `/api/registration/tasks/{uuid}/cancel` | 取消任务 |
| GET | `/api/registration/available-services` | 可用邮箱服务 |

### 账号管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/accounts` | 账号列表（支持分页、筛选、搜索） |
| GET | `/api/accounts/{id}` | 账号详情 |
| PATCH | `/api/accounts/{id}` | 更新账号（状态/cookies） |
| DELETE | `/api/accounts/{id}` | 删除账号 |
| POST | `/api/accounts/batch-delete` | 批量删除 |
| POST | `/api/accounts/export/json` | 导出 JSON |
| POST | `/api/accounts/export/csv` | 导出 CSV |
| POST | `/api/accounts/export/cpa` | 导出 CPA 格式（单文件或 ZIP） |
| POST | `/api/accounts/export/sub2api` | 导出 Sub2API 格式 |
| POST | `/api/accounts/{id}/refresh` | 刷新 Token |
| POST | `/api/accounts/batch-refresh` | 批量刷新 Token |
| POST | `/api/accounts/{id}/validate` | 验证 Token |
| POST | `/api/accounts/batch-validate` | 批量验证 Token |
| POST | `/api/accounts/{id}/upload-cpa` | 上传单账号到 CPA |
| POST | `/api/accounts/batch-upload-cpa` | 批量上传到 CPA |
| POST | `/api/accounts/{id}/upload-sub2api` | 上传单账号到 Sub2API |
| POST | `/api/accounts/batch-upload-sub2api` | 批量上传到 Sub2API |

### 支付升级

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/payment/generate` | 生成 Plus/Team 支付链接 |
| POST | `/api/payment/open` | 后端无痕模式打开浏览器 |
| POST | `/api/payment/accounts/{id}/mark-subscription` | 手动标记订阅类型 |
| POST | `/api/payment/accounts/batch-check-subscription` | 批量检测订阅状态 |
| POST | `/api/payment/accounts/{id}/upload-tm` | 上传单账号到 Team Manager |
| POST | `/api/payment/accounts/batch-upload-tm` | 批量上传到 Team Manager |

### 邮箱服务

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/email-services` | 服务列表 |
| POST | `/api/email-services` | 添加服务 |
| PATCH | `/api/email-services/{id}` | 更新服务 |
| DELETE | `/api/email-services/{id}` | 删除服务 |
| POST | `/api/email-services/{id}/test` | 测试服务 |
| POST | `/api/email-services/outlook/batch-import` | 批量导入 Outlook |

### 上传服务管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/api/cpa-services` | CPA 服务列表/创建 |
| PUT/DELETE | `/api/cpa-services/{id}` | 更新/删除 CPA 服务 |
| POST | `/api/cpa-services/{id}/test` | 测试 CPA 连接 |
| GET/POST | `/api/sub2api-services` | Sub2API 服务列表/创建 |
| PUT/DELETE | `/api/sub2api-services/{id}` | 更新/删除 Sub2API 服务 |
| POST | `/api/sub2api-services/{id}/test` | 测试 Sub2API 连接 |
| GET/POST | `/api/tm-services` | Team Manager 服务列表/创建 |
| PUT/DELETE | `/api/tm-services/{id}` | 更新/删除 TM 服务 |
| POST | `/api/tm-services/{id}/test` | 测试 TM 连接 |

### 设置

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/settings` | 获取所有设置 |
| POST | `/api/settings/proxy/dynamic` | 更新动态代理设置 |
| GET/POST/DELETE | `/api/settings/proxies` | 代理列表管理 |
| POST | `/api/settings/proxies/{id}/set-default` | 设为默认代理 |
| GET | `/api/settings/database` | 数据库信息 |

### WebSocket

| 路径 | 说明 |
|------|------|
|| `ws://host/api/ws/logs/{uuid}` | 实时日志流 |

## Docker 部署

### 环境要求

- Docker
- Docker Compose

### 快速部署

```bash
# 克隆项目
git clone https://github.com/cnlimiter/codex-register.git
cd codex-register

# 启动服务
docker-compose up -d
```

服务启动后访问 http://localhost:8000

### 配置说明

**端口映射**：默认 `8000` 端口，可在 `docker-compose.yml` 中修改。

**数据持久化**：
```yaml
volumes:
  - ./data:/app/data
  - ./logs:/app/logs
```

**环境变量配置**：
```yaml
environment:
  - APP_ACCESS_PASSWORD=mypassword
  - APP_HOST=0.0.0.0
  - APP_PORT=8000
```

### 常用命令

```bash
# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down

# 重新构建
docker-compose build --no-cache
```

## 注意事项

- 首次运行会自动创建 `data/` 目录和 SQLite 数据库
- 所有账号和设置数据存储在 `data/register.db`
- 日志文件写入 `logs/` 目录
- 代理优先级：动态代理 > 代理列表（随机/默认） > 直连
- CPA / Sub2API / Team Manager 上传始终直连，不走代理；其中 CPA 可选把账号记录的代理写入 auth file 的 `proxy_url`
- 注册时自动随机生成用户名和生日（年龄范围 18-45 岁）
- 支付链接生成使用账号 access_token 鉴权，走全局代理配置
- 无痕打开支付页默认调用系统 Chrome/Edge 的隐私模式
- 订阅状态自动检测调用 `chatgpt.com/backend-api/me`，走全局代理
- 批量注册并发数上限为 50，线程池大小已相应调整

## License

[MIT](LICENSE)
