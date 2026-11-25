# 构建

## 环境

- Node.js 18+（宿主插件运行与测试）
- Python 3.10+（构建脚本与引擎桥）
- 无需网络

## 生成 lib/

```powershell
python build/build_lib.py
```

`build/build_lib.py` 做两件事：

1. 把根目录的运行时文件（`index.js`、`cordis.patch.yml`、`icon.svg`、`engine/`、`prompts/`、`locale/`）
   复制到 `lib/`（**不要把 `package.json` 放进 `lib/`**，见下方说明）；
2. 调用 `build/emit-client.py`，以 `ui/econtone-studio.html` 为唯一源生成 `lib/client.js` 与
   `lib/standalone.html`。

`lib/` 是**已提交的发布产物**：安装本包时不需要构建。不要手改 `lib/` 下的文件（除排错外），
重新构建会覆盖。

> 注意：`lib/` 里不能有 `package.json`。DSH 的 client-modules 会以「离模块最近的 package.json」
> 计算客户端产物路径（`dirname(pkg.json) + exports["./client"]`），多一份清单会把路径解析成
> `lib/lib/client.js`，导致插件启动失败。宿主读取版本时依次尝试 `./package.json` 与 `../package.json`。

## 校验

```powershell
node --check lib/client.js
python -m py_compile engine/studio_bridge.py
npm test
```

`npm test` 运行 `tests/smoke.mjs`（node:test）：检查清单字段、`lib/` 产物是否齐全、客户端模块注册、
宿主路由、引擎桥命令。

## 作者自检

```powershell
npx dsh-insights-kit selfcheck . --lang zh
```

退出码 0 表示没有 fail 级扣分；1 表示存在 fail 项。

## 版本

- 版本号写在 `package.json`；
- 发布前同时更新 `ui/econtone-studio.html` 里的版本徽标，再执行 `python build/build_lib.py`；
- 面板会定时比对 `/build` 返回的版本，发现不一致时提示刷新。
