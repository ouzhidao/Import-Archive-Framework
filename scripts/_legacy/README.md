# scripts/_legacy/ — 初版 demo 遗留脚本（不要在生产流程里跑）

> 2026-10-15 归位：这些脚本是项目初版 demo 阶段的产物，**不属于通用管线**，移到这里避免误用。

## 清单

| 脚本 | 为什么是 legacy |
|---|---|
| `clean_demo.py` | 硬编码 E 盘微信路径 + 写死一票（LS-24-01-01）；且含 `shutil.rmtree(base)`（对目标目录有删除动作）→ **有数据风险，勿跑** |
| `test_vision.py` | 冒烟测试脚本，硬编码 `work/render26B/...` 单张图片路径；用于当年选 vision 模型 |

## 通用管线在哪

| 模块 | 在 `scripts/` |
|---|---|
| M2 归票 / M4 索引卡 / M5 校验 | `pipeline_core.py` |
| M3 OCR（百度主轨） | `ocr_vision.py` |
| M7 落盘（读 index → L1-L4 复制） | `build_archive_sample.py` |
| 报关单去价 | `mask_customs_price.py` |
| 票总览 HTML | `ticket_overview.py` |
| **只读审计（三方差异 + R7/R5）** | `audit_archive.py` |
