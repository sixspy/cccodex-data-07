# cc-data-lab

CC_Codex 二期（26.11）**数据工程**主线基线：**采集任务控制台**（Flask 后端 + HTMX 前端页 + 站内 Mock 目录）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数据采集、爬虫与 RPA  

## 技术栈

- Python 3.12+、Flask 3、SQLite  
- HTMX（CDN）+ Jinja 模板  
- pytest  

## 本地运行

```powershell
cd claude-workspace\cc-data-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

浏览器打开 http://127.0.0.1:5590 ，登录 **admin / 123456**。

流程：创建任务 → 选种子 `/mock/catalog/alpha`、`beta` 或 `gamma`（gamma 含两端空白和短题名）→ 详情页按「取回 → 修整 → 入账」逐道执行 → 查看中转筐与入账册。

## 三道工序与续做规矩

一次采集拆成三道工序，依次执行，每道各自留下进度（`job_stages` 表）：

| 工序 | 做什么 | 落点 |
|------|--------|------|
| 取回 fetch | 把站点原文行原样写入中转筐，不碰入账册 | `staging_items` |
| 修整 clean | 抹掉题名两端空白；不足 4 字的行停在中转筐并记下缘故，不放行 | `staging_items.status/reason` |
| 入账 commit | 只写修整已放行的行 | `crawl_records` |

规矩：

1. **只有后台裁决**：能否进入下一道、本道能否再点，全部由 `app/services/runner.py`
   的 `_gate` / `_claim` 判定；页面按钮只是提示，绕过按钮直接 POST 一样被拒。
2. **前一道没完成，后一道不可用**：上一道状态不是 `done` 时，本道按钮 disabled，后台返回
   「上一道工序「…」尚未完成，不能进入「…」」。
3. **同一道还在忙时拒绝再点**：状态为 `running` 时后台返回「正在执行，请稍后再试」，
   占位更新是带 `state IN ('pending','failed')` 条件的单条 UPDATE，避免并发重复执行。
4. **只从没做成的那道往下接**：某道未做成时状态记 `failed` 并落 `reason`；已经 `done`
   的工序不能被重跑。续做时不会重新取回中转筐里已有的原文 —— 取回按
   `UNIQUE(job_id, sku)` 用 `INSERT OR IGNORE`，入账同理按 `UNIQUE(job_id, item_key)`，
   所以条数不会变成两份。
5. **三道都做成，任务才结束**：入账成功后任务状态为 `done`，入账条数等于修整放行的行数。
   中途离开再回来，详情页仍显示每道的状态、计数与没做成的缘故。

### 页面上怎样从失败的那一道接着做

1. 打开任务详情页，看三张工序卡片：出问题的那张顶上标「未做成」，卡片内红字写着**没做成的缘故**。
2. 这张卡片的按钮会变成橙色「从「修整」中断处继续」（以实际工序名为准），点它即可续做；
   它前面已完成的工序按钮是灰的「已完成」，后面的工序仍不可用。
3. 续做成功后，同一道变为「已完成」，下一道的按钮自动变为可点，继续按顺序点下去即可。
4. 如果整条链路想一次跑完，也可用兼容入口 `POST /jobs/<id>/run`：它从第一道未完成的工序往下接，
   遇到未做成的那道就停下并在页面顶部提示缘故。


## Docker 一键启动（推荐用于快照前验收）

需已打开 **Docker Desktop**：

```powershell
cd claude-workspace\cc-data-lab
docker compose up -d --build
docker compose ps
```

- 页面：http://127.0.0.1:5590/login  
- Mock：http://127.0.0.1:5590/mock/catalog/alpha  
- 停止：`docker compose down`（保留库卷）· 清卷：`docker compose down -v`

`docker compose ps` 中 `STATUS` 含 **healthy** 表示服务可访问。线下表「环境可复现等级」可填 **已容器化，可一键起环境**。

## 前后端说明

| 层 | 说明 |
|----|------|
| 后端 | `app/` 路由、任务状态机、同步 runner、SQLite |
| 前端 | `templates/` + `static/`，浏览器可点操作 |
| Mock 源站 | `/mock/catalog/<id>` HTML，无需外网 |

## 初始快照

在首次 Claude 单轮跑题前 commit + push 本树（不含 `.venv/`、`instance/*.db`）。后续题目在同 repo 上叠加调度、RPA 步骤、增量水位等能力。

## 基线 vs 跑题目录

| 路径 | 角色 |
|------|------|
| `claude-workspace/cc-data-lab`（本目录） | **基线**：改功能、docker 验收 |
| `ccproject/cc-data-lab/data-XX` | **单题**：复制基线后挂容器、出快照 |

复制：`..\scripts\Copy-BaselineToQuestion.ps1 -QuestionId data-01`

## 跑题 Harness

见 `CCcodex/docs/ClaudeCode_Windows使用说明.md`（镜像 2.0）。容器挂载 **ccproject 下该题目录**，不是本基线路径。
