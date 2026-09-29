# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值和履约状态和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、例外审定和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表（旧库会自动补列与索引）。

## 角色

- `intake_officer`：受理经办，创建记录、执行评估、发起例外申请。
- `exception_reviewer`：例外复核人，独立审定例外申请。
- `underwriter`：审批人，凭有效例外批准纾困方案。
- `servicer`：履约管理人，生效/恢复/违约登记。
- `admin`：可执行任意角色动作，但四眼原则（发起人不能复核自己的例外）对所有人生效。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/exceptions`：发起例外申请，`data`为`{"reason":"...","expires_at":"YYYY-MM-DD"}`。
- `GET /api/records/{id}/exceptions`：该贷款的例外列表。
- `GET /api/exceptions/{id}`：例外详情，含`history`例外专属时间线。
- `POST /api/exceptions/{id}/review`：复核审定，请求体为`{"expected_version":1,"data":{"decision":"approve|reject","review_note":"..."}}`（驳回必须填写意见）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 例外审定规则

偿付能力评估不通过（`eligibility=false`）的贷款不能直接批准，必须走独立的例外审定：

1. **发起**：经办人填写理由（不少于5字）和到期日（必须晚于当天）；仅评估完成且不满足偿付能力的贷款可发起。
2. **唯一待审**：同一贷款同时只能存在一张开口例外（待审中、或已通过尚未被方案使用）；驳回/失效/已使用后才能再次发起，由数据库部分唯一索引兜底。
3. **双人复核**：另一名复核人（`exception_reviewer`）通过或驳回；发起人不能复核自己的申请，对`admin`同样生效；复核使用独立版本号做乐观并发。
4. **放行**：复核通过后，审批人（`underwriter`）批准时必须在`data.exception_id`引用本笔贷款的例外；例外须为通过状态、未被使用、未到期。例外随批准被原子占用（`consumed_record_version`），不能重复放行其他方案。
5. **失效**：到期日过后例外在下次访问时惰性失效（`voided/expired`）；方案登记违约时，该贷款已通过的例外（含用于批准本方案的那张）联动失效（`voided/defaulted`）。失效后不能再据此批准。
6. **可追溯**：例外的发起、复核、占用、失效全部写入贷款审计时间线和例外专属`history`；详情可见状态、发起人、复核人、到期日、失效原因与时间。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及例外审定的双人复核、自审拦截、唯一待审、过期失效、违约联动、跨贷款引用和时间线。
