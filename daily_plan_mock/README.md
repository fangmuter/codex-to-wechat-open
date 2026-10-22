# Phase 1.1 本机 Mock

这是隔离的每日计划状态机验证，不连接微信、Codex、API Key、网络或监听端口。仅使用 Python 标准库与 SQLite。

运行测试：`python -m unittest discover -s daily_plan_mock/tests -p 'test_*.py'`

状态覆盖每日触发、跳过/暂停、四类输入、草稿、精确确认、幂等、outbox、恢复和紧急停用。真实微信窗口、二维码、Codex 登录、调度常驻仍未验证。
