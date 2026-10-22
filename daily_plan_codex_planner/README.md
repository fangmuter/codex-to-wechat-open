# Phase 1.5 Stage A planner boundary

`daily_plan_codex_planner` is deliberately not wired to `ilink_receive_probe` or
`daily_plan_wechat_integration`.  It accepts only explicit planning fields and
returns validated data or the fixed Phase 1.4 structured-input fallback.

Stage A has `ALLOWED_MODEL = None`; no live model turn can be started.  A future
approval must select one runtime-discovered model, after which its App Server turn
must use `workspace/` as its only readable root, `readOnly`, and
`approvalPolicy=never`.  The App Server documentation does not establish an
equivalent network-off guarantee for that read-only profile.

The App Server currently has no verified built-in `tools = none` control.  This is
intentionally represented as a preflight blocker, not relaxed by prompt wording.
