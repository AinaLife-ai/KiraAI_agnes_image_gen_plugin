# Agnes AI 图片生成插件

[![KiraAI](https://img.shields.io/badge/KiraAI-插件-blue)](https://github.com/znq19/KiraAI)
[![版本](https://img.shields.io/badge/version-1.0.0-green)]()
[![Python](https://img.shields.io/badge/Python-3.10+-yellow)]()

> 让 KiraAI 的 Bot 能通过 Agnes AI 快速生成高质量（大概是吧）图片，**自动发送到聊天**，全程无需手动操作。

## 它能做什么

- 用户说 "画一只猫" → Bot 自动生成图片 → 直接发到群/私聊
- 用户说 "发张你的自拍" → Bot 用自己的形象参考图，生成角色图 → 自动发送
- 支持文生图、图生图、角色形象图三种模式

## 特性

- 🎨 **4 种风格** × **5 种尺寸**：动漫 / 写实 / 油画 / 水彩，正方形到手机竖屏
- 🤖 **角色形象图**：配置一张 Bot 角色参考图，用户说 "你长什么样" 就自动图生图
- 📨 **合并转发**：多张图用 QQ 合并转发发送，不刷屏
- 🔄 **失败自动回退**：合并转发失败 → 自动切换逐张发送
- 🔁 **智能重试**：API 繁忙时自动等几秒重试，最多 3 次
- 💾 **缓存管理**：生成图自动保存，超出上限自动清理旧文件
- 🌐 **代理支持**：配置 HTTP 代理即可

## 安装

1. 下载插件文件夹
2. 放到 KiraAI 的 `data/plugins/` 目录下：

```text
KiraAI/
  data/
    plugins/
      agnes_image_gen/     ← 放这里
        main.py
        manifest.json
        schema.json
        requirements.txt
```

3. 打开 KiraAI WebUI → 插件管理 → 找到 "Agnes AI 图片生成" → 启用
4. **填写 API Key**（必填）：

> API Key 是 `sk-` 开头的密钥，从 Agnes AI 获取。没有的话去 [Agnes AI](https://apihub.agnes-ai.com) 注册。（目前纯免费白嫖）

## 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| API Key | Agnes AI 密钥（必填） | 空 |
| 默认尺寸 | 1024×1024 / 竖图 / 横图 / 宽屏 / 手机竖屏 | 1024×1024 |
| 默认风格 | 动漫 / 写实 / 油画 / 水彩 | 动漫 |
| 单次最大张数 | 一次生成几张 | 4 |
| 角色形象参考图 | Bot 角色自身的形象参考图路径 | 自动读取 KiraAI 设置 |
| 合并转发 | 多张图是否合并转发（仅 QQ） | 开启 |
| 保留生成图片 | 发送后是否保留本地文件 | 开启 |
| 缓存上限 | 最多保留多少张本地图 | 100 |

> 💡 **角色形象参考图**：留空会自动读取 KiraAI 系统设置的 selfie 图片。填了就用你填在插件这的。

## 使用示例

### 文生图

```
用户：帮我画一只在樱花树下的白猫，动漫风
Bot： [调用工具] → [生成] → [发送] → "樱花树下的白猫来啦"
```

### 角色形象图

```
用户：你长什么样？发张自拍看看
Bot： [use_selfie=true] → [用角色参考图生成] → [发送] → "喏，这就是我"
```

### 图生图

```
用户：基于这张图，帮我换个赛博朋克风格的背景
Bot： [reference_image_url=用户发的图的URL] → [生成] → [发送]
```

## 依赖

- Python 3.10+
- aiohttp

KiraAI 会在首次加载时自动安装依赖。

## License

AGPLv 3.0
