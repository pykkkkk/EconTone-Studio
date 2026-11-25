# Contributing

欢迎 issue 与 PR。提交前请先读完本文件。

## 开发环境

- Node.js 18+
- Python 3.10+
- DeepSeek Harness 0.2.x，可用 EconTone skill pack（`econtone-score`、`econtone-polish`、
  `econtone-ai-trace`、`econtone-prosody`、`econtone-equilibrium`）

## 改哪里

- 界面与交互：只改 `ui/econtone-studio.html`（唯一设计源）；
- 宿主接口与调度：改 `index.js`；
- 引擎桥：改 `engine/studio_bridge.py`；
- 改完运行 `python build/build_lib.py` 重新生成 `lib/`（宿主副本 + 客户端 + 独立页）。

## 提交前

```powershell
python build/build_lib.py
node --check lib/client.js
python -m py_compile engine/studio_bridge.py
npx dsh-insights-kit selfcheck . --lang zh
```

- `selfcheck` 退出码必须为 0（无 fail 级扣分）；
- 不要提交个人路径、会话内容或未脱敏截图；
- PR 描述写清楚：改了什么、为什么、如何验证。

## 许可

提交即表示同意以 MIT 许可证发布。
