# 故障预测温度取数平台

为故障预测训练样本补齐**预测当时可知**的温度，严格避免未来信息泄漏。
技术栈：FastAPI + PostgreSQL + SQLAlchemy，浏览器页面 + JSON API，Docker Compose 一键启动
（PostgreSQL + **两个对等 web 实例** web1/web2，共享同一数据库）。

## 30 秒启动

```bash
docker compose up --build
# 打开浏览器（两个实例等价，任择其一）
open http://localhost:8001        # web1；web2 为 http://localhost:8002
```

容器首次启动会自动：

1. 在 PostgreSQL 建表（含对旧库的轻量在线迁移：补 `status` 等列）；
2. 两个实例同时启动时，演示数据加载以行锁/互斥串行，**只有一个实例写入**演示数据
   （仅当数据库还没有任何输入时，幂等，重启不会覆盖后续上传）；
3. 在首页即可点击「发起取数」，再点取数单查看分页结果，或勾选取数单进行「清退」。

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
- 「发起取数」分两步：先在一个短事务内把当前两个数据集 ID **固定（pin）** 到一张
  `PENDING` 取数单并写入**心跳租约**（`job_leases`），随后用独立短事务物化全部结果行到
  `fetch_results`，完成后取数单转为 `READY` 并释放租约。之后再上传/替换任何文件都不会改变
  已生成的取数单；固定之后、结果尚未生成期间，租约也会独占保护其旧输入。
- 计算线程每 10 秒续租，租约 TTL 默认 30 秒（`JOB_LEASE_TTL_SECONDS` 可调）。
  **计算进程退出后心跳停止，TTL 过期即视为死亡**：下次清退会把该取数单标记为 `FAILED`、
  解绑其集引用，使其独占的旧集可以被回收。
- 分页为按 `sample_id` 的 **keyset 游标分页**（`WHERE sample_id > :cursor ORDER BY sample_id`），
  每个 HTTP 请求使用独立短会话。同游标重试、服务重启后的后续页均内容恒定，不重不漏；
  清退其他取数单不影响保留单的任何游标页。

## 清退（弃用数据回收）

首页「历史取数单与清退入口」勾选**至多 20 张**取数单提交，整个清退在**单个数据库事务**内完成，
任一步失败则整次回滚。处理顺序：

1. 删除勾选且存在的取数单及其物化结果行；**重复清退已删除的编号不报错**，只在报告中列为跳过；
   对租约仍存活的计算中取数单跳过（不等待其计算结束）；
2. 回收计算进程已退出（租约过期）的计算中取数单：标记 `FAILED` 并解绑旧集引用；
3. 对每个样本集/修订集判断：是**当前输入**则跳过；仍被**剩余取数单**（含租约存活的计算中取数单
   固定的旧输入）引用则跳过并注明引用来源；其余连同明细行（`samples`/`revisions`）一并删除。

清退报告页显示删除的取数单数、结果行数、回收的集数与明细行数，并逐条列出跳过对象及原因
（当前输入 / 保留取数单引用 / 计算中取数单已固定的旧输入 / 取数单不存在 等）。

## 页面与接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 首页：当前输入状态、上传/替换、发起取数、取数单清单与清退勾选 |
| POST | `/uploads/samples` | 整份替换样本 CSV（multipart 字段 `file`） |
| POST | `/uploads/revisions` | 整份替换读数修订 CSV |
| POST | `/jobs` | 发起取数（固定输入+租约+物化），303 跳转到取数单页面 |
| GET | `/jobs/{id}?cursor=&limit=` | 取数单网页分页（默认 50，上限 200）；计算中返回 409 自动刷新页，已删返回 404 |
| GET | `/api/jobs/{id}/results?cursor=&limit=` | JSON 分页，返回 `status/items/next_cursor/has_more` |
| POST | `/retirements` | 清退表单（字段 `job_ids`，多个值或逗号分隔，至多 20 个），返回清退报告网页 |
| GET | `/api/retirements/preview` | 清退前预览：全部取数单状态、存活租约、当前输入集 |
| GET | `/healthz` | 健康检查 |

## 本地开发与自测（无需 Docker，用 SQLite 跑测试）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q          # 50 个用例：解析/还原/端到端/分页/不可变/清退/租约/双实例交错
DATABASE_URL="sqlite+pysqlite:////tmp/app.db?timeout=30" LOAD_DEMO_ON_START=1 \
  .venv/bin/uvicorn app.main:app --port 8000  # 本地起服务
```

生产/Compose 默认使用 PostgreSQL（`postgresql+psycopg://...`），SQLite 仅用于测试，
时间戳在两种后端上都以带时区 UTC 存取（见 `app/db.py` 的 `UTCDateTime`），
心跳租约、行锁均为可移植实现，不依赖任何后端专有语法。

## 目录结构

```
app/
  parsing.py      # CSV 解析与严格校验（拒收即抛 CSVValidationError）
  logic.py        # 纯函数：逐样本点-in-time 温度还原
  db.py           # SQLAlchemy 模型、版本化输入、pin/租约/物化、keyset 分页、轻量迁移
  job_runner.py   # 取数执行：心跳租约线程 + 独立短事务物化
  retirement.py   # 清退：删单、回收死单、按引用回收集与明细、跳过原因报告
  demo_loader.py  # 幂等且并发安全的演示数据加载
  main.py         # FastAPI 路由与页面
  templates/      # Jinja2 页面（首页/取数单/计算中/清退报告）
demo/             # 演示数据与非法样例
tests/            # pytest 自测（含线程级双实例交错与租约过期场景）
docker-compose.yml / Dockerfile
```
