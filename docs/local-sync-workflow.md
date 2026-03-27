# 本地同步工作流（Zoe 定制版）

目标：
- `upstream` 跟踪官方仓库：`https://github.com/cnlimiter/codex-manager.git`
- `origin` 指向你的 fork：`https://github.com/cnjerry828/codex-manager.git`
- `upstream-sync` 作为本地上游同步基线
- `custom/zoe` 作为你的长期自定义分支（保留导入代理、切换网关等功能）

## 当前分支职责

- `upstream-sync`
  - 仅镜像 `upstream/master`
  - 不直接做业务修改

- `custom/zoe`
  - 你的长期可运行分支
  - 合并上游更新后，继续保留你的自定义功能

- `custom/zoe-local-YYYYMMDD-HHMMSS`
  - 某次同步前的安全快照
  - 用于回滚/找回本地改动

## 日常同步步骤

```bash
cd /home/jerry/.openclaw/workspace/scripts/codex-manager

git fetch upstream --prune

git checkout upstream-sync
git reset --hard upstream/master

git checkout custom/zoe
git merge upstream-sync
```

如果合并有冲突：
1. 优先保留你的定制功能（导入代理、切换网关、本地部署相关）
2. 同时尽量吸收上游的新接口和新修复
3. 解决后执行：

```bash
git add -A
git commit
```

## 推送到自己的 GitHub

首次推送：

```bash
git push -u origin custom/zoe
```

如果你想让 fork 的默认分支就是自定义分支，也可以在 GitHub 页面把默认分支切到 `custom/zoe`。

## 推荐使用方式

部署/运行时，优先使用：

```bash
git checkout custom/zoe
```

这样你的功能不会因为切回纯上游分支而“消失”。

## 需要注意

- 不要再直接把长期定制堆在 `master` 上
- `master` / `upstream-sync` 用来观察上游变化
- 真正运行建议固定在 `custom/zoe`
- 每次大同步前，先新建一个安全快照分支

## 典型大同步前的安全操作

```bash
git checkout custom/zoe
git checkout -b custom/zoe-backup-$(date +%Y%m%d-%H%M%S)
git checkout custom/zoe
```

## 当前已保留的自定义方向

- 代理导入功能
- 切换网关脚本与注册流程集成
- 本地配置文件（如 `config.json`、`proxies.txt`）

后续如果新增自定义功能，建议：
- 提交信息写清楚用途
- 尽量集中修改少量文件
- 能独立脚本化的功能尽量脚本化，减少和上游核心逻辑冲突
