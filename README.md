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
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情，包含例外审定列表和当前有效例外。
- `GET /api/records/{id}/audit`：审计时间线。
- `POST /api/records/{id}/exceptions`：经办人为不满足偿付能力的已评估方案发起例外审定。
- `GET /api/records/{id}/exceptions`：查看该贷款的例外审定列表。
- `GET /api/exceptions/{id}`：查看单张例外审定详情。
- `POST /api/exceptions/{id}/review`：另一名例外复核人复核通过或驳回。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

### 独立例外审定

- 发起人角色为`intake_officer`，必须填写`reason`和晚于当天的`expires_on`（`YYYY-MM-DD`）。
- 复核人角色为`exception_reviewer`，必须是不同于发起人的用户；复核请求提供例外当前`expected_version`、`decision`（`approved`/`rejected`）和`review_note`。
- 同一贷款最多保留一张`pending`例外；驳回或到期后才能重新发起。
- 只有`approved`且未到期的例外能让`underwriter`批准不满足偿付能力的方案；原`approve`动作不再接受一键`exception_approved`参数。方案审批人也不能是该例外的发起人或复核人。
- 例外到期会自动转为`expired`；已批准方案发生`default`时，关联有效例外转为`invalidated`。
- 例外详情保存状态、发起人、复核/审批人、理由、复核意见、到期日和失效原因；时间线记录发起、复核、到期、方案批准引用和违约失效全过程。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、独立例外审定、到期/违约失效、重复引用、权限拒绝和版本冲突。
