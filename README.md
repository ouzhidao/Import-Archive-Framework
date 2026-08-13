# 进口单证归档框架（方法论 · 可推 Git）

> 进口鱼糜/鱼片单证自动化整理框架。**本仓库只存方法/规范/脚本/模板**；真实业务单据留本地，永不进 Git。
> 数据边界见 `.gitignore` 与 `CLAUDE.md`。

## 目录

| 目录 | 内容 | 进 Git |
|---|---|---|
| `config/` | 业务规则配置（供应商/单据/路由/规则/路径） | ✅ |
| `scripts/` | 管线模块（OCR/归票/索引/校验/落盘） | ✅ |
| `docs/` | 借鉴与前瞻设计文档 | ✅ |
| `schema/` | 索引卡 JSON Schema 等 | ✅ |
| `templates/` | 审核初稿模板 | ✅ |
| `runtime/` | **运行时数据**（真实单证/缓存/统计/样本） | ❌ |
| `.env` | 密钥（百度OCR/邮箱） | ❌ |

## 快速开始

1. 读 `CLAUDE.md`（铁律 + 架构 + 运行 + 业务口径）
2. 复制 `.env.example` 为 `.env` 填百度 OCR 密钥（A主/B备）
3. `python scripts/ocr_vision.py <pdf>` 验证 OCR
4. `python scripts/build_archive_sample.py` 生成落盘样本
5. `python scripts/ticket_overview.py` 生成票总览 HTML 查看器

## 核心文档

- `CLAUDE.md` — 项目指引（必读，含业务口径：日期/发票明细/捕捞区域/必备矩阵）
- `docs/命名规范.md` — 命名铁律（L4/文件名/友好标签/号码）
- `docs/OCR实战借鉴_标签审核项目.md` — OCR 防坑（缩图/质检/配额/页级索引）
- `docs/草稿审核工作流_前瞻设计.md` — 二期审核前瞻
- `docs/新规则三步校验法.md` — 加规则前校验
- `config/*.yaml` — 全部业务规则（改规则不改代码）

## 人侧界面（票总览 HTML）

- 数据源：`runtime/tickets/*_index.json` → `scripts/ticket_overview.py` → 自包含 HTML
- 分层筛选 + 供应商/物料/年份 + 关键词搜索 + 发票明细 + 生产日期/捕捞区域 + 定位目录
- 借鉴 `原料标签库` 的 html_report.py 模式

## 数据红线

真实单据只读不写、只复制不移动、小批量落盘；真实税号/账号永不写入；数据层永不进 Git。
