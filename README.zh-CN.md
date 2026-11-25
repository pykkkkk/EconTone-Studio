# EconTone Studio

[![version](https://img.shields.io/badge/version-1.0.0-4D6BFE?style=flat-square&labelColor=1F1F1F)](https://github.com/pykkkkk/EconTone-Studio/releases)
[![dsh](https://img.shields.io/badge/dsh-0.2.x-4D6BFE?style=flat-square&labelColor=1F1F1F)](https://github.com/deepseek-ai/deepseek-harness)
[![license](https://img.shields.io/badge/license-MIT-4D6BFE?style=flat-square&labelColor=1F1F1F)](LICENSE)

![EconTone Studio](docs/social-preview.png)

English ｜ [简体中文](README.zh-CN.md)

一个基于博弈思想的经管论文精修工作台，运行在 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 里。

![评分卡](docs/screenshots/01-score.jpg)

![润色稿与词级差异](docs/screenshots/02-rewrite.jpg)

![逐条对比](docs/screenshots/05-compare.jpg)

![均衡证书](docs/screenshots/03-certificate.jpg)

![运行历史](docs/screenshots/04-history.jpg)

## 安装

```powershell
git clone https://github.com/pykkkkk/EconTone-Studio.git
```

```json
"dependencies": {
  "econtone-studio": "link:D:/path/to/EconTone-Studio"
},
"dsh": {
  "profile": {
    "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", "econtone-studio"]
  }
}
```

重新加载 DSH。引擎默认从 `~/.dsh/skills` 读取 skill，可用 `engineRoot` 覆盖。

## 目录

```
├─ index.js                 宿主：接口、运行调度、状态同步
├─ ui/econtone-studio.html  界面源文件（唯一手改入口）
├─ engine/studio_bridge.py  调用 EconTone skill 脚本的桥
├─ lib/                     发布产物（已提交，安装无需构建）
├─ build/build_lib.py       源码 → lib/
├─ tests/                   node:test 快速测试
└─ docs/                    架构、构建说明、图片
```

## 开发

```powershell
python build/build_lib.py
node --test tests/smoke.mjs
```

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/BUILDING.md](docs/BUILDING.md) · [CHANGELOG.md](CHANGELOG.md) · [CHANGELOG-DEV.md](CHANGELOG-DEV.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md)

## 许可

[MIT](LICENSE)
