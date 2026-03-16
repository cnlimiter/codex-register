# OpenAI 自动注册系统 v2

自动化注册 OpenAI 账号的 Web UI 系统，支持多种邮箱服务、批量注册、代理管理和账号管理。

## 功能特性

- **多邮箱服务支持**
  - Tempmail.lol（临时邮箱，无需配置）
  - Outlook（IMAP + XOAUTH2，支持批量导入）
  - 自定义域名（REST API）

- **注册模式**
  - 单次注册
  - 批量注册（可配置数量和间隔时间）
  - Outlook 批量注册（指定账户逐一注册）

- **实时监控**
  - WebSocket 实时日志推送
  - 跨页面导航后自动重连
  - 降级轮询备用方案

- **代理管理**
  - 静态代理配置
  - 动态代理（通过 API 每次获取新 IP）
  - 代理列表（随机选取，记录使用时间）

- **账号管理**
  - 查看、删除、批量操作
  - Token 刷新与验证
  - 导出（JSON / CSV / CPA 格式）
  - CPA 上传（Codex Protocol API）

- **系统设置**
  - 代理配置（静态 + 动态）
  - Outlook OAuth 参数
  - 注册参数（超时、重试、密码长度等）
  - 验证码等待配置
  - 数据库管理（备份、清理）

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

### 启动 Web UI

```bash
# 默认启动（127.0.0.1:8000）
python webui.py

# 指定地址和端口
python webui.py --host 0.0.0.0 --port 8080

# 调试模式（热重载）
python webui.py --debug
```

启动后访问 http://127.0.0.1:8000

## Docker 部署（推荐）

### 环境要求

- Docker
- Docker Compose

### 快速部署

```bash
# 克隆项目
git clone https://github.com/db52/codex-register.git
cd codex-register

# 启动服务
docker-compose up -d
```

服务启动后访问 http://localhost:8000

### Docker Compose 说明

默认配置：
- 端口：8000
- 数据持久化：`./data` 目录
- 日志持久化：`./logs` 目录

### 使用代理

如需在容器中使用代理，编辑 `docker-compose.yml`：

```yaml
services:
  codex-register:
    environment:
      - HTTP_PROXY=http://your-proxy:port
      - HTTPS_PROXY=http://your-proxy:port
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
│   ├── core/           # 核心功能（注册引擎、HTTP 客户端、CPA 上传）
│   ├── database/       # 数据库（SQLAlchemy + SQLite）
│   ├── services/       # 邮箱服务实现
│   └── web/            # FastAPI Web 应用
│       ├── app.py      # 应用入口、路由挂载
│       ├── routes/     # API 路由
│       └── websocket.py # WebSocket 处理
├── templates/          # Jinja2 HTML 模板
├── static/             # 静态资源（CSS / JS）
└── data/               # 运行时数据目录（数据库、日志）
```

## 技术栈

| 层级 | 技术 |
|------|------|
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | SQLAlchemy + SQLite |
| 模板引擎 | Jinja2 |
| HTTP 客户端 | curl_cffi（浏览器指纹模拟） |
| 实时通信 | WebSocket（websockets 库） |
| 前端 | 原生 JavaScript（无框架） |
| 打包 | PyInstaller |

## API 端点

### 注册任务

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/registration/start` | 启动单次注册 |
| POST | `/api/registration/batch` | 启动批量注册 |
| GET | `/api/registration/batch/{id}` | 批量任务状态 |
| POST | `/api/registration/batch/{id}/cancel` | 取消批量任务 |
| POST | `/api/registration/outlook-batch` | 启动 Outlook 批量注册 |
| GET | `/api/registration/outlook-batch/{id}` | Outlook 批量状态 |
| GET | `/api/registration/tasks` | 任务列表 |
| GET | `/api/registration/tasks/{uuid}` | 任务详情 |
| GET | `/api/registration/tasks/{uuid}/logs` | 任务日志 |
| POST | `/api/registration/tasks/{uuid}/cancel` | 取消任务 |
| DELETE | `/api/registration/tasks/{uuid}` | 删除任务 |
| GET | `/api/registration/available-services` | 可用邮箱服务 |
| GET | `/api/registration/outlook-accounts` | 可用 Outlook 账户 |

### 账号管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/accounts` | 账号列表 |
| GET | `/api/accounts/{id}` | 账号详情 |
| DELETE | `/api/accounts/{id}` | 删除账号 |
| POST | `/api/accounts/batch-delete` | 批量删除 |
| POST | `/api/accounts/export/json` | 导出 JSON |
| POST | `/api/accounts/export/csv` | 导出 CSV |
| POST | `/api/accounts/export/cpa` | 导出 CPA 格式 |
| POST | `/api/accounts/{id}/refresh` | 刷新 Token |
| POST | `/api/accounts/batch-refresh` | 批量刷新 Token |
| POST | `/api/accounts/{id}/validate` | 验证 Token |
| POST | `/api/accounts/batch-validate` | 批量验证 Token |
| POST | `/api/accounts/{id}/upload-cpa` | 上传到 CPA |
| POST | `/api/accounts/batch-upload-cpa` | 批量上传到 CPA |

### 邮箱服务

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/email-services` | 服务列表 |
| POST | `/api/email-services` | 添加服务 |
| GET | `/api/email-services/{id}` | 服务详情 |
| PATCH | `/api/email-services/{id}` | 更新服务 |
| DELETE | `/api/email-services/{id}` | 删除服务 |
| POST | `/api/email-services/{id}/test` | 测试服务 |
| POST | `/api/email-services/outlook/batch-import` | 批量导入 Outlook |

### 设置

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/settings` | 获取所有设置 |
| POST | `/api/settings/proxy` | 更新代理设置 |
| POST | `/api/settings/dynamic-proxy` | 更新动态代理设置 |
| POST | `/api/settings/cpa` | 更新 CPA 设置 |
| POST | `/api/settings/cpa/test` | 测试 CPA 连接 |
| GET | `/api/settings/database` | 数据库信息 |

### WebSocket

| 路径 | 说明 |
|------|------|
| `ws://host/api/ws/task/{uuid}` | 单任务实时日志 |
| `ws://host/api/ws/batch/{id}` | 批量任务实时状态 |

## 注意事项

- 首次运行会自动创建 `data/` 目录和 SQLite 数据库
- 所有账号和设置数据存储在 `data/register.db`
- 日志文件写入 `logs/` 目录
- 代理设置优先级：动态代理 > 代理列表（随机） > 静态默认代理
- 注册时自动随机生成用户名和生日（年龄范围 18-45 岁）
