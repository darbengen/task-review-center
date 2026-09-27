# 任务中心 MCP

[下载最新版安装包](https://github.com/darbengen/task-review-center/releases/latest) · [MIT 开源许可](LICENSE)

把 Codex 的任务整理为「待审查 / 已完成」，ChatGPT 聊天显示在独立第三栏。支持确认、撤销、重点、备注、搜索及 Work 任务归档。

这是 darben 的自建插件，并非 OpenAI 官方产品。分享包不包含作者的任务、聊天、备注、登录信息或密钥。无需另填 API Key，也不调用付费模型接口。

## 最省事的安装方法

需要 macOS、Python 3.9 或更新版本，以及支持插件和 MCP 全页入口的 Codex 桌面版。后台仅使用 Python 标准库，无需 pip 安装依赖。Windows 不支持；Linux 未验证。

1. 解压，把 `task-review-center` 文件夹放到长期保留的位置，安装后不要移动或删除。
2. 将文件夹交给 Codex，并复制下面这段话：

> 请帮我安装这个任务中心插件。先阅读文件夹里的 README.md，运行 python3 prepare.py 生成本机启动路径及运行副本。使用你可用的 plugin-creator 技能，把此插件注册到本机 personal marketplace；保留现有插件和配置，遇到同名插件先检查来源，不覆盖。然后用 codex plugin add task-review-center@实际市场名 安装。不要复制别人的任务数据，不要修改官方数据库，不要重启正在执行任务的应用。安装后验证 MCP 初始化、工具列表与页面资源读取，并告诉我在哪里打开任务中心。

## 手动准备和接入

在解压后的 `task-review-center` 目录打开终端：

```sh
python3 prepare.py
```

这个命令只改当前文件夹的 `.mcp.json` 和 `runtime/`，不会改 Codex 的配置或已有任务。生成的 `.mcp.json` 可以作为 stdio MCP 配置；其中 command 和 args 已是当前电脑的真实路径。

要在 Codex 中出现插件入口，还需登记 personal marketplace 并安装。建议交给上面的 Codex 安装提示词处理。本包不自动覆盖 `~/.agents/plugins/marketplace.json`。只有将 MCP 加进普通客户端，不能保证出现完整三栏页面。

安装后打开新对话，输入「打开任务中心」，或在应用的「探索 / 更多」中寻找入口。首次安装或旧连接未更新时，等正在执行的任务结束后，再完全退出并重开 Codex。能否固定到一级侧栏由宿主版本决定。

## 使用与数据

- 「确认」是本地审查标记，不表示 AI 已完成任务，也不会发消息。
- Work 归档会调用 Codex 的归档接口，界面提供二次确认；Chat 栏不支持归档。
- 数据保存在自己的 `~/Library/Application Support/CodexTaskReview/`，不是解压目录。确认、重点和备注有本地历史备份。
- 默认通过 `~/.codex/app-server-control/app-server-control.sock` 读取 Work 任务；Chat 只读 `~/.codex/sqlite/codex-dev.db` 的目录元数据及 `.codex-global-state.json` 中的目录账号标识，不读取聊天正文或登录凭据，也不写官方数据库。
- Chat 原文链接打开 chatgpt.com 对话；Work 使用 Codex 会话链接。
- 连接不可用会提示并保留已有记录。插件运行时约 8 秒同步；关闭时不会持续监控。

## 兼容性与排障

原版在 macOS Codex 26.903.61454 环境开发。分享包做隔离后端回归及启动测试，不代表已在接收者电脑或所有 Codex 版本验证。

- 提示找不到 Python：安装 Python 3.9+，重新运行 `python3 prepare.py`。
- Work 无法读取：先打开 Codex 和一个聊天；本版本依赖宿主控制 socket，并非每个发行版都提供。
- Chat 无法读取：先打开应用的聊天列表，等待目录同步；部分发行版没有相应索引表或账号目录，不能保证第三栏可用。
- 页面入口不出现：检查插件已启用，使用新对话；普通 MCP 客户端可能只支持工具，不支持全页入口。
- 解压后移动了目录：重新运行 prepare.py 并重新安装插件。不要手改已发布的 runtime 文件。
- 应用升级后失败：保留本地审查目录，先检查接口兼容性；不要清空任务数据来修复连接问题。

卸载请在 Codex 插件管理中完成，本地审查记录默认保留。安装和验证无需归档任何真实任务。

## 开发验证

```sh
python3 -m unittest discover -s tests -v
```

测试使用临时目录和模拟目录数据。核心源码与原版一致；分享版仅调整版本、配置准备与交付文档。本项目采用 [MIT License](LICENSE)，允许使用、修改和分发，请保留版权及许可声明。

## 星号就是置顶

标星后排在所在栏的非标星项前面，取消星号恢复原排序；多个置顶项保持原有时间排序。搜索与范围筛选仍然有效。
