# ilink_receive_probe

Phase 1.2A 的离线 receive-only probe。只提供 login mock、接收 mock batch、白名单、去重、cursor、重启恢复、超时和紧急停用；没有真实微信、真实网络、Codex、业务出站、媒体、命令或监听器。

运行：`python -m unittest discover -s ilink_receive_probe/tests -p 'test_*.py'`。

Windows 凭据保护未实现：标准库不能可靠提供 DPAPI/Credential Manager；Phase 1.2B 前必须单独审批安全存储方案，绝不退回明文 JSON。
