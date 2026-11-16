# 故障预测温度取数平台

为故障预测训练样本补齐**预测当时可知**的温度，严格避免未来信息泄漏。
技术栈：FastAPI + PostgreSQL + SQLAlchemy，浏览器页面 + JSON API，Docker Compose 一键启动。

## 30 秒启动

```bash
docker compose up --build
# 打开浏览器
open http://localhost:8000        # macOS；Linux 用 xdg-open，或直接访问
```

容器首次启动会自动：

1. 在 PostgreSQL 建表；
2. 载入 `demo/` 下的演示数据（仅当数据库还没有任何输入时，幂等，重启不会覆盖后续上传）；
3. 在首页即可点击「发起取数」，再点取数单查看分页结果。

## 输入文件格式（均为 UTF-8 CSV，首行表头，时间为带时区的 ISO-8601 UTC）

样本 CSV（最多 1000 行，`sample_id` 全表唯一）：

| 列名 | 含义 |
| --- | --- |
| `sample_id` | 唯一样本号 |
| `device_id` | 设备号 |
| `prediction_time` | 预测时刻（如 `2026-01-15T10:20:00Z`） |

读数修订 CSV（最多 10000 行）：

| 列名 | 含义 |
| --- | --- |
| `reading_id` | 读数号；同一读数号的 `device_id`、`measurement_time` 固定 |
| `device_id` | 设备号 |
| `measurement_time` | 测量时刻 |
| `known_time` | 获知时刻，**全局唯一** |
| `temperature` | 温度；撤销行必须留空 |
| `revoked` | 撤销标记：`true/false`（也接受 `1/0`、`yes/no`） |

演示与拒收样例见 `demo/samples.csv`、`demo/revisions.csv`、`demo/samples_bad.csv`、`demo/revisions_bad.csv`。

## 取数规则（无未来信息泄漏）

对每个样本 `(device_id, prediction_time)`：

1. **逐读数还原状态**：只看 `known_time <= prediction_time` 的修订，取其中 `known_time` 最大的一条；
   若该条是撤销（`revoked=true`），该读数视为不存在——**撤销不回退到旧修订**；
2. **选最近有效读数**：在同设备、还原后仍有效、且 `measurement_time <= prediction_time` 的读数中，
   取测量时刻最晚者；测量时刻相同取 `reading_id` 字典序最小者；
3. 没有合格读数时温度**留空**，来源也留空；否则附上产生该读数当前状态的来源修订
   `source_revision_id = "<reading_id>#<known_time>"`。

## 一致性设计（不依赖长期数据库事务）

- 每次上传都生成一个**版本化的新数据集**，全部行插入成功后才在单事务内切换「当前输入」指针；
  解析或入库失败则整事务回滚，**非法文件被拒收，原输入保持不变**。
- 「发起取数」会快照当前两个数据集的 ID，并在**一个短事务内物化全部结果行**到 `fetch_results`；
  之后再上传/替换任何文件都不会改变已生成的取数单（首屏固定两份输入，后续更正不能改变该取数单）。
- 分页为按 `sample_id` 的 **keyset 游标分页**（`WHERE sample_id > :cursor ORDER BY sample_id`），
  每个 HTTP 请求使用独立短会话。同游标重试、服务重启后的后续页均内容恒定，不重不漏。

## 页面与接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 首页：当前输入状态、上传/替换、发起取数、历史取数单 |
| POST | `/uploads/samples` | 整份替换样本 CSV（multipart 字段 `file`） |
| POST | `/uploads/revisions` | 整份替换读数修订 CSV |
| POST | `/jobs` | 发起取数，303 跳转到取数单页面 |
| GET | `/jobs/{id}?cursor=&limit=` | 取数单网页分页（默认 50，上限 200） |
| GET | `/api/jobs/{id}/results?cursor=&limit=` | JSON 分页，返回 `items / next_cursor / has_more` |
| GET | `/healthz` | 健康检查 |

## 本地开发与自测（无需 Docker，用 SQLite 跑测试）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q          # 30 个用例：解析校验/还原规则/端到端/分页/不可变
DATABASE_URL="sqlite+pysqlite:////tmp/app.db" LOAD_DEMO_ON_START=1 \
  .venv/bin/uvicorn app.main:app --port 8000  # 本地起服务
```

生产/Compose 默认使用 PostgreSQL（`postgresql+psycopg://...`），SQLite 仅用于测试，
时间戳在两种后端上都以带时区 UTC 存取（见 `app/db.py` 的 `UTCDateTime`）。

## 目录结构

```
app/
  parsing.py      # CSV 解析与严格校验（拒收即抛 CSVValidationError）
  logic.py        # 纯函数：逐样本点-in-time 温度还原
  db.py           # SQLAlchemy 模型、版本化输入、取数单物化、keyset 分页
  demo_loader.py  # 幂等演示数据加载
  main.py         # FastAPI 路由与页面
  templates/      # Jinja2 页面
demo/             # 演示数据与非法样例
tests/            # pytest 自测
docker-compose.yml / Dockerfile
```
