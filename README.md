# 编排机器人能力降级与恢复计划基础平台

本项目是一套可离线运行的 Python 服务端平台，服务于具身智能机器人控制域、AI 计算域和国产电子部件质量域。平台管理控制计算节点、实时控制总线、资源时隙、机器人构建、验证测量、分析租约、部件批次与质量决定，业务状态、幂等结果和审计事件保存在 SQLite 中。

控制域还提供**能力降级编排**：把任务所需能力、部件健康摘要版本、安全限制和可替代执行链冻结成带 SHA-256 的计划版本，计算「可继续 / 受限运行 / 等待人工 / 安全停机」四种解释性结论。确认计划时在同一事务内原子锁定替代链的控制资源；健康摘要版本变化会使未终态计划（含草稿与执行中计划）失效并释放锁。执行回执按 `(计划,回执编号)` 幂等去重、按观测时间拒绝乱序回执，已进入安全终态的机器人不会被迟到的「正常」回执恢复。值班后台按机器人展示当前缺失能力、采用的替代链、未完成人工动作和恢复前必须重新满足的证据。

## 目录

- `src/robot_control/`：控制节点、实时总线、资源批次、时隙申请、容量分配、架构情景和能力降级编排（`degradation.py` 为纯计算，`service.py` 为事务用例）；
- `src/embodied_ai/`：机器人构建、验证协议、测量导入、排除复核、分析任务和准入决定；
- `src/component_qualification/`：国产电子部件批次、信号测量、统计分析、账号权限和质量审批；
- `fixtures/`：离线验收使用的验证协议与结构化测量；
- `tests/`：领域规则、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m robot_control.acceptance --workspace .
PYTHONPATH=src python3 -m embodied_ai.acceptance --workspace .
PYTHONPATH=src python3 -m component_qualification.acceptance
```

三条命令会在临时 SQLite 数据库中完成控制资源分配、AI 验证分析和国产电子部件质量流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m robot_control.api --database robot-control.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m embodied_ai.api --database embodied-ai.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_qualification.api --database component.sqlite3 --host 127.0.0.1 --port 8082
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

### 能力降级编排接口（8080）

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| POST | `/health/snapshots` | `operator` | 登记部件健康摘要（自动版本化、相同内容去重）；新版本使该机器人未终态计划失效 |
| POST | `/degradation/plans` | `operator` | 基于最新健康摘要冻结计划版本（任务能力、安全限制、替代链、人工动作），返回解释性结论 |
| GET | `/degradation/plans/{plan_id}` | 无额外鉴权 | 读取计划（含解释、锁、人工动作与回执记录） |
| POST | `/degradation/plans/{plan_id}/confirm` | `dispatcher` | 确认计划，同事务原子锁定替代链控制资源；`safe_stop` 结论直接进入安全终态 |
| POST | `/degradation/plans/{plan_id}/manual-actions/{action_id}` | `operator` | 登记人工动作完成证据；补偿证据齐备后结论由等待人工推进为受限运行 |
| POST | `/degradation/plans/{plan_id}/receipts` | `operator` | 登记执行回执（normal/degraded/safe_stop）；重复幂等、乱序忽略、安全终态不可恢复 |
| GET | `/degradation/board` | 调度值班角色 | 所有机器人降级总览：缺失能力、替代链、待办人工动作、恢复前置证据 |
| GET | `/degradation/robots/{robot_id}` | 无额外鉴权 | 单台机器人降级视图 |

冻结计划的请求体示例见 `tests/test_degradation.py` 与 `src/robot_control/acceptance.py`。
