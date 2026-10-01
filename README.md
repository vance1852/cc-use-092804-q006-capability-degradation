# 编排机器人能力降级与恢复计划基础平台

本项目是一套可离线运行的 Python 服务端平台，服务于具身智能机器人控制域、AI 计算域和国产电子部件质量域。平台管理控制计算节点、实时控制总线、资源时隙、机器人构建、验证测量、分析租约、部件批次与质量决定，业务状态、幂等结果和审计事件保存在 SQLite 中。

## 目录

- `src/robot_control/`：控制节点、实时总线、资源批次、时隙申请、容量分配、架构情景和能力降级编排；
- `src/embodied_ai/`：机器人构建、验证协议、测量导入、排除复核、分析任务和准入决定；
- `src/component_qualification/`：国产电子部件批次、信号测量、统计分析、账号权限和质量审批；
- `fixtures/`：离线验收使用的验证协议与结构化测量；
- `tests/`：领域规则、事务、权限、HTTP API 和命令行验收测试。

## 能力降级编排

`robot_control` 域内置能力降级编排：把任务所需能力、部件健康摘要、安全限制和可替代执行链冻结成计划版本（`inputs_sha256` 可复核），由确定性引擎计算**可继续 / 受限运行 / 等待人工 / 安全停机**四档解释性结论。

- 确认计划时在单个事务中原子锁定所需控制资源（容量不足整体回滚），并取代旧计划、释放旧锁；
- 健康摘要、任务、安全限制或执行链变化会递增上下文版本，使未执行计划失效并释放其锁；
- 执行回执按幂等键去重，乱序与过期计划的回执被拒绝；已进入安全终态（`safe_stopped`）的机器人不会被迟到的正常回执恢复，只能经恢复计划在证据重新满足后退出终态；
- 恢复证据（部件重新恢复健康的新观测、人工动作完成）在恢复计划提出与确认时双重校验；
- `GET /degradation/robots/{id}/status` 与 `GET /degradation/board` 展示每台机器人当前缺失能力、采用的替代链、未完成的人工动作和恢复前必须重新满足的证据。

主要接口：`POST /degradation/robots`、`/degradation/robots/{id}/components|chains|task|health|plans`、`POST /degradation/plans/{id}/confirm|receipts`、`POST /degradation/actions/{id}/complete`、`POST /degradation/resources`。角色分工：planner 登记目录，dispatcher 上报健康、提出计划、提交回执与完成人工动作，risk 确认计划，auditor 读取看板与审计链。

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
