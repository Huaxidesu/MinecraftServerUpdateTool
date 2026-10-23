# Minecraft Server Update Tool

> Minecraft 整合包的服务端清单分发 + 玩家端增量更新工具。纯 Python 标准库，零第三方依赖。

![Python](https://img.shields.io/badge/python-%E2%89%A53.8-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)
![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-red)

## 简介

把整合包目录交给**导出器**扫描，生成一份带 SHA-1 校验的 `server-manifest.json`，连同整合包文件一起推送到 git 仓库；玩家运行**更新器**，自动对比版本、校验哈希、只下载差异文件，并在全部成功后一次性生效。支持玩家跨任意多个版本跳跃升级，历史删除的文件一个不漏。

## 特性

- **增量更新**：按文件哈希对比，只下载新增/变动的文件，多线程下载（默认 5，最高 16）
- **跨版本清理**：`removedFiles` 累积机制，玩家从 v1.0 直跳 v1.5 也能删掉中间版本废弃的文件；重新加回的文件自动撤销废弃，不与后续更新冲突
- **原子生效**：所有文件下载校验通过后才更新本地记录，中途失败不留半成品
- **大小写自愈**：Windows 大小写不敏感导致的 404，自动经 git API 反查真实路径并永久修正
- **玩家保护名单**：`options.txt`、journeymap 等玩家数据永不覆盖、永不误删；玩家还可自定义 `keepFiles`
- **更新器自更新**：更新器自身也在清单里，玩家每次更新顺带升级更新器
- **导出加速**：文件大小 + 纳秒 mtime 缓存哈希，重复导出秒级完成
- **中文交互**：面向玩家的提示全部中文化，网络错误自动翻译成可读原因

## 工作原理

```
打包方                                git 仓库                       玩家端
─────────────────                  ──────────────────           ─────────────────
manifest_exporter.py               server-manifest.json          updater.py
  扫描整合包目录          push ──▶  server-extra.json    ◀── 拉取    对比版本与哈希
  计算 SHA-1                        全部整合包文件                  清理废弃文件
  生成/累积清单                                                 多线程下载差异
  同步更新器副本                                                全部成功才落盘
```

哈希规则：二进制文件直接对原始字节做 SHA-1；文本文件先统一 CRLF → LF 再做 SHA-1，保证跨平台一致。

## 仓库结构

```
├── lib/
│   ├── manifest_exporter.py           # 导出器（打包方）
│   └── manifest-exporter-config.json  # 导出器配置（首次运行自动生成）
├── client/
│   ├── update/updater.py              # 玩家端更新器（唯一源头）
│   └── 更新整合包.bat                  # 玩家端入口
├── 导出脚本.bat                        # 导出器入口
├── server-extra.json                  # 保护名单 / 强制删除配置样例
└── tests/                             # 回归测试
```

## 部署教程

### 一、准备托管仓库

1. 在任意支持 **raw 文件直链** 的 git 托管上建仓库（推荐自建 Gitea）。
2. 确认 raw 地址格式，这就是 `fileApi`：
   - Gitea：`https://<host>/<owner>/<repo>/raw/branch/main/`（**推荐**，支持 404 大小写自愈）
   - GitHub：`https://raw.githubusercontent.com/<owner>/<repo>/main/`（可下载，但大小写自愈不可用）
3. 仓库需允许匿名读取（玩家端无凭据拉取）。

### 二、部署导出器（打包方）

1. 把本仓库克隆到本地。
2. 准备一份完整的整合包目录（内含 `config/`、`mods/` 等）。
3. 首次运行 `导出脚本.bat`，按提示输入：
   - 整合包目录（扫描源）
   - 导出目录（git 仓库本地克隆的根目录）
   - fileApi（上一步确认的 raw 地址）
   - 其余项回车取默认即可
4. 配置写入 `lib/manifest-exporter-config.json`，之后每次运行直接复用。
5. 导出完成后按提示 `git add / commit / push`，玩家即可更新。

> 每次发布新版本的流程 = 改整合包 → 运行 `导出脚本.bat` → 确认新版本号 → push。

### 三、打包客户端（给玩家）

发布给玩家的压缩包按此结构组织：

```
整合包/
├── 更新整合包.bat        # 来自 client/
├── server-manifest.json # 基线清单（版本固定 1.0，由导出器自动维护）
├── update/
│   ├── updater.py       # 导出器已自动同步的最新更新器
│   └── python/          # 可选：内置 Python，免去玩家装环境
└── （config/、mods/ 等整合包内容可放可不放，玩家首次更新会补齐）
```

玩家侧操作：解压 → 双击 `更新整合包.bat` → 首次运行粘贴自己的 `.minecraft`（或版本隔离）目录 → 等待完成。

### 四、更新记录与配置文件位置

| 文件 | 位置 | 作用 |
|---|---|---|
| `update/server-manifest.json` | **游戏目录的 update 子目录** | 本地更新记录（版本号 + 文件清单 + 更新器版本） |
| `update/.server-manifest-temp.json` | 游戏目录的 update 子目录（隐藏） | 下载中的临时清单，正常运行结束自动删除 |
| `update/updater-config.json` | 整合包 `update/` 目录 | 玩家端设置（游戏目录路径、线程数、keepFiles 等） |
| `server-extra.json` | 整合包根目录 | 服务器下发的保护名单，有新版自动更新 |

> 记录统一收进游戏目录的 `update/` 子目录，避免放在根下被玩家清理目录时误删。2.4 之前的版本把更新记录放在整合包根目录，升级后首次运行会自动迁移。

## 配置参考

### server-extra.json（保护名单）

```jsonc
{
  "version": "2",
  "noOverwriteFiles": ["options.txt", "journeymap/", "shaderpacks/*.txt"],
  "forceDelete": ["config/broken.cfg"]
}
```

- `noOverwriteFiles`：永不覆盖/删除；支持目录前缀（`xxx/`）与 `*` 通配
- `forceDelete`：每次更新前强制删除（处理历史遗留坏文件）

### updater-config.json（玩家端，一般无需手改）

```jsonc
{
  "minecraftPath": "D:/.../.minecraft",
  "updaterVersion": "2.4",
  "downloadThreads": 5,
  "verbose": false,
  "keepFiles": ["mods/my-own-mod.jar"]
}
```

- `downloadThreads`：1–16
- `verbose`：排障时改 `true`，打印大小写校正等诊断明细
- `keepFiles`：玩家自加文件的永久保护名单

## 常见问题

**Q：玩家从很旧的版本直接更新，中间版本删掉文件会残留吗？**
不会。导出器把历史废弃文件累积进 `removedFiles` 随清单发布，更新器每次都照单清理，与玩家起点版本无关。

**Q：下载一半断网会怎样？**
网络类错误自动重试 3 次（递增间隔）；仍失败则终止本次更新，**不会**写入本地记录，下次运行继续。

**Q：玩家自己装的 mod 会被删吗？**
不会。清理只针对清单记录过的文件；不放心还可以写进 `keepFiles`。

**Q：提示 404 / 找不到文件？**
多为仓库内文件名大小写与清单不一致。使用 Gitea 托管时更新器会自动反查修正并永久写回；GitHub raw 无此能力，请保证推送时大小写一致。

## 开发

- 回归测试：`python tests/test_updater.py`
- 核心约定：导出器与更新器的哈希算法、清单格式是公共契约，改动必须两侧同步

## 许可证

本项目采用 [PolyForm Noncommercial License 1.0.0](LICENSE)。

- ✅ 允许：个人/非商业场景的使用、修改、再分发（需附带协议文本）
- ❌ 禁止：**任何商业用途**（包括但不限于出售、付费服务、公司内部生产使用）
