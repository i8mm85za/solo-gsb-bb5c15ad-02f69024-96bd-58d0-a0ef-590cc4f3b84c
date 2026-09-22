# 多评委评分校准（Multi-Judge Score Calibration）

一个用于校准多评委评分偏差、识别人名前后名次反转并支持人工裁决审计的 Web 应用。

- **后端**：FastAPI（Python 3.12）+ SQLite，数据**仅保存在 Docker 卷**中
- **前端**：原生 HTML / CSS / JavaScript（无构建步骤、无框架）
- **算法**：按评委（或评委×项目）做 **中位数 + MAD** 稳健标准化
- **审计**：每条评分可标记「保留原分 / 采用校准分」并填写理由，排名实时重算，可导出含完整审计明细的 CSV

---

## 一条命令启动

```bash
docker compose up --build -d
```

启动后访问：

> **http://localhost:8000**

停止：

```bash
docker compose down          # 停止，保留数据卷
docker compose down -v       # 停止并删除数据卷（清空全部数据）
```

> 若 8000 端口被占用，可修改端口（见下文「配置」）。

---

## 功能一览

1. **CSV 导入**：上传包含 `选手、评委、项目、分数` 的 CSV（支持中英文表头，自动识别 UTF-8 BOM）。
2. **一键载入示例数据**：内置 6 位选手 × 4 位评委 × 3 个项目（72 条评分），其中包含刻意设置的宽松/严苛评委与人名前后名次反转（S03/S04 名次互换）。
3. **稳健标准化**
   - 对每个校准分组计算中位数与 MAD（median absolute deviation）：
     ```
     Z = (原始分 − 分组中位数) / (分组MAD × 1.4826)
     校准分 = 全局中位数 + Z × 全局MAD(×1.4826)
     ```
   - 系数 1.4826 使 MAD 在正态分布下是标准差的一致估计量；分组无离散度（MAD=0）时取 Z=0。
   - 校准范围可在 **「按评委（跨项目汇总）」** 与 **「按评委×项目」** 间切换。
4. **合成项目总分**：选手每个项目取各评委打分的均值，项目总分为各项目均值之和。
5. **名次反转识别**：列出所有在原始排名与校准排名之间优胜关系反转的选手对及分差。
6. **逐项人工裁决**：每条评分可选择「采用校准分」（默认）或「保留原分」并填写理由；保存后**最终排名实时重算**（原始与校准排名始终并列展示）。
7. **导出审计 CSV**（UTF-8 BOM，Excel 可直接打开），包含四个部分：
   - 【明细】逐条评分：原始分、分组中位数/MAD、稳健 Z、校准分、采用选择、最终分、理由
   - 【汇总】选手原始/校准/最终总分与名次、名次变化
   - 【反转】标准化前后名次反转的选手对
   - 【校准参数】各分组中位数与 MAD
8. **排名稳定性实验（临时模拟，不保存）**：选择**临时排除一名评委或一个项目**，系统按当前校准范围重新拟合校准参数并重算原始、校准、最终总分与竞赛名次；逐位展示每位选手相对基线的三类名次变化、三类总分变化以及被移除的评分条数，并汇总名次上升/下降人数、最大总分波动等。实验结果仅保存在浏览器当前页面，**不会写入会话裁决或数据库**，可随时「清除结果，恢复基线」；上传数据、切换校准范围或修改裁决时实验结果会自动失效。
9. **清空会话**：一键删除当前浏览器会话的全部评分、裁决与理由（数据仅存在容器卷，删除不可恢复）。

---

## CSV 文件格式

必需 4 列，表头顺序任意，以下表头名均可识别（不区分大小写、忽略空格）：

| 字段 | 支持的表头（节选） |
|---|---|
| 选手 | `选手`、`选手编号`、`姓名`、`contestant`、`name` |
| 评委 | `评委`、`裁判`、`judge`、`juror`、`rater` |
| 项目 | `项目`、`赛项`、`环节`、`item`、`event`、`round` |
| 分数 | `分数`、`得分`、`评分`、`score`、`points` |

示例：

```csv
选手,评委,项目,分数
S01,评委甲,技术展示,6.85
S01,评委乙,技术展示,16.23
S02,评委甲,技术展示,7.31
...
```

校验规则：四列缺一不可；分数必须为数字；同一「选手 × 评委 × 项目」不允许重复；空行会被忽略。错误信息会指到具体行号。

---

## 配置

通过环境变量（`.env` 文件或 shell 环境）配置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `APP_PORT` | `8000` | 宿主机映射端口（容器内固定 8000） |
| `CALIB_DATA_DIR` | `/data` | 容器内数据库目录（已挂载 Docker 卷，一般无需修改） |

例如改用 9000 端口：

```bash
echo "APP_PORT=9000" > .env
docker compose up --build -d
# 访问 http://localhost:9000
```

持久化：SQLite 数据库位于命名卷 `calibration-data:/data/calibration.db`。升级镜像（`docker compose up --build`）不会丢数据；仅当显式 `docker compose down -v` 时才删除。

不使用 Compose 时：

```bash
docker build -t judge-calibration .
docker run -d -p 8000:8000 -v calibration-data:/data --name judge-calibration judge-calibration
```

---

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
CALIB_DATA_DIR=./data uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
pip install pytest httpx
CALIB_DATA_DIR=$(mktemp -d) python -m pytest tests/ -q
```

测试覆盖稳健统计量、总分与并列名次、反转检测、排除评委/项目的稳定性实验、CSV 解析/导出以及完整 API 流程（上传 → 裁决 → 稳定性实验 → 切范围 → 导出 → 清空）。

---

## API 摘要

会话 ID 由浏览器在 `localStorage` 生成并随路径传递（`{session_id}` 为任意字符串）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| POST | `/api/sessions/{id}/upload?scope=judge` | 上传 CSV（multipart `file`） |
| POST | `/api/sessions/{id}/sample?scope=judge` | 载入内置示例数据 |
| GET | `/api/sessions/{id}/analysis` | 获取完整分析结果（分组参数、总分、名次、反转、逐条分数） |
| POST | `/api/sessions/{id}/scope` | 切换校准范围（`{"scope":"judge"\|"judge_item"}`） |
| PATCH | `/api/sessions/{id}/scores/{score_id}/decision` | 单条裁决（`{"use_raw":bool,"reason":str}`），返回实时重算结果 |
| POST | `/api/sessions/{id}/stability` | 排名稳定性实验（`{"exclude_type":"judge"\|"item","exclude_name":str}`），返回基线 vs 排除后逐位名次/总分变化；只读，不落库 |
| POST | `/api/sessions/{id}/decisions/reset` | 清除全部裁决，统一恢复校准分 |
| GET | `/api/sessions/{id}/export` | 下载审计 CSV |
| DELETE | `/api/sessions/{id}` | 清空会话 |

自动生成的交互式 API 文档：`http://localhost:8000/docs`。

---

## 算法说明

- 总分聚合方式为**项目内先对评委求均值，再对项目求和**，因此项目之间等权；如某选手在某项目缺少评委评分，该项目按实际到的评委取均值。
- MAD 标准化只在每个分组内部拟合尺度（中位数/MAD），再统一映射到全局中位数与全局 MAD 的尺度上；校准分可以出现负值或超出原始量程，这是对极端宽/严评委做稳健降权的正常表现。
- 名次使用**竞赛排名**（1,2,2,4，同分并列）。
- 「最终总分」= 对每条评分应用人工裁决（校准分或保留原分）后重新聚合的结果，用于实时排名；原始排名与纯校准排名不受裁决影响，始终保留以便对照。
- **排名稳定性实验**在内存中复制一份评分并剔除被排除评委/项目的全部评分（被剔除评分上的裁决一并忽略），随后用同一套 `analyze` 管线重新拟合分组与全局中位数/MAD 并重新聚合排名，再与基线逐位对比；选手在剩余数据中无任何评分时标记为「无法计算名次」。整个过程不执行任何数据库写操作。

## 目录结构

```
.
├── app/
│   ├── main.py            # FastAPI 路由
│   ├── scoring.py         # 中位数/MAD 标准化、总分、排名、反转检测
│   ├── csv_io.py          # CSV 解析与审计导出
│   ├── database.py        # SQLite 持久化（/data 卷）
│   └── static/            # 原生前端（index.html / app.js / styles.css）
├── samples/sample_scores.csv
├── tests/                 # 单元 + API 测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
