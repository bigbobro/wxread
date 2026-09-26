# 更新第三版，提高读书稳定程度，最大限量保持cookie有效时间，欢迎fork！

## 项目介绍 📚

这个脚本主要是为了在微信读书的阅读**挑战赛中刷时长**和**保持天数**。由于本人偶尔看书时未能及时签到，导致入场费打了水漂。网上找了一些，发现高赞的自动阅读需要挂阅读器模拟或者用ADB模拟，实现一点也不优雅。因此，我决定编写一个自动化脚本。通过对官网接口的抓包和JS逆向分析实现。

该脚本具备以下功能：

- **阅读时长调节**：默认计入排行榜和挑战赛，时长可调节，默认为60分钟。
- **定时运行推送**：可部署在GitHub Action/服务器上，支持每天定时运行并推送结果到微信。
- **Cookie自动更新**：脚本能自动获取并更新Cookie，一次部署后面无需其它操作。
- **轻量化设计**：本脚本实现了轻量化的编写，部署服务器/GIthub action后到点运行，无需额外硬件。

***
## 操作步骤 🛠️

### 抓包准备

脚本逻辑还是比较简单的，`main.py`与`push.py`代码不需要改动。在微信阅读官网 [微信读书](https://weread.qq.com/) 搜索【三体】点开阅读点击下一页进行抓包，抓到`read`接口 `https://weread.qq.com/web/book/read`，如果返回格式正常（如：

```json
{
  "succ": 1,
  "synckey": 564589834
}
```
右键复制为Bash格式。

### 方法一： GitHub Action部署运行（GitHub运行）


- Fork这个仓库后，先打开 fork 的 **Actions** 页面，启用 `wxread` workflow；公开仓库 fork 后，定时 workflow 默认不会自动启用。
- 在仓库 **Settings** -> 左侧列表中的 **Secrets and variables** -> **Actions**，然后在右侧的 **Repository secrets** 中添加如下值：
  - `WXREAD_CURL_BASH`：上面抓read接口后转换为curl_bash的数据。
  - `PUSH_METHOD`：推送方法，4选1推送方式（pushplus、wxpusher、telegram、serverChan）。
  - `PUSHPLUS_TOKEN` or `WXPUSHER_SPT` or `TELEGRAM_BOT_TOKEN`&`TELEGRAM_CHAT_ID` or `SERVERCHAN_SPT`: 选择推送后填写对应token。
  
- 在 **Variables** 部分，最下方添加变量：
  - `READ_NUM`：设定每天阅读的目标次数，每次 30 秒；例如 `180` 为每天 90 分钟。

#### 本 fork 的自动补跑

2026-09-27 起，原来每天 12:07 的单次任务增加了当天进度恢复和两次补跑检查，时间均为北京时间：

| 时间 | 行为 |
| --- | --- |
| 12:07 | 执行当天阅读任务 |
| 16:17 | 检查当天进度，只补尚未完成的次数 |
| 18:27 | 再检查一次；当天达到目标后直接跳过 |

- 沿用现有登录和推送 Secrets，无需新增 Key。主任务、补跑和手动触发共享当天目标，串行执行，后来的任务不取消正在阅读的任务。
- 每个带有效 `synckey` 的成功响应确认后保存次数；运行结束后，无论阅读成功还是脚本报错，都尝试上传当天进度。进度文件只包含日期、版本和已完成次数，作为 GitHub Actions artifact 保留 7 天，不包含 Cookie、请求内容或推送凭证。
- 阅读请求遇到暂时性网络错误、HTTP 429/5xx 或无效响应时，最多额外重试 3 次，分别等待 5、15、30 秒。Cookie 连续刷新和 synckey 连续修复各限 3 次。持续失败会保留进度，等待后面的补跑时段。
- 单次阅读最多运行约 120 分钟，工作流总上限 150 分钟，为上传进度和通知留出时间。跨过北京时间午夜时停止旧日期任务，不把旧日期的进度计入新一天。
- 先保存阅读进度，再推送结果。通知失败不会把已完成阅读清零，也不会触发整段重读。推送按服务返回的业务结果判断是否接受请求，不把 HTTP 200 一律当成成功；通知仍以接收端实际收到为准。失败通知包含已确认的分钟数、原因和 Actions 链接；当天已完成的补跑不重复推送。
- GitHub 调度本身仍可能延迟或漏触发。进度 API 不可读或数据损坏时会停止本次阅读，避免把未知进度当成零。Runner 被强制终止、进度上传失败、或服务端已处理请求但响应丢失时，不能保证所有次数都被准确恢复，也不能保证完全不重复计时。
- 进度代表脚本收到确认的次数，最终阅读时长和挑战赛结果以微信读书端为准。本地/Docker 运行默认仍每次执行目标次数；仅设置 `WXREAD_STATE_FILE` 后才启用文件续跑。

`Verify recovery` 工作流使用模拟请求测试中断续跑、当天去重、重试上限和日期切换，并通过真实 GitHub artifact 上传/下载检查进度传递；不会读取登录 Secrets 或调用微信读书接口。


- 基本释义：

| key                        | Value                               | 说明                                                         | 属性      |
| ------------------------- | ---------------------------------- | ------------------------------------------------------------ | --------- |
| `WXREAD_CURL_BASH`         | `read` 接口 `curl_bash`数据 | **必填**，必须提供有效指令                                   | secrets   |
| `READ_NUM`                 | 每天的目标次数（每次 30 秒）         | **可选**，默认 40 次/20 分钟；补跑只完成剩余次数             | variables |
| `PUSH_METHOD`              | `pushplus`/`wxpusher`/`telegram`/`serverchan`    | **可选**，推送方式，4选1，默认不推送                                       |    secrets     |
| `PUSHPLUS_TOKEN`           | PushPlus 的 token                   | 当 `PUSH_METHOD=pushplus` 时必填，[获取地址](https://www.pushplus.plus/uc.html) | secrets   |
| `WXPUSHER_SPT`             | WxPusher 的token                    | 当 `PUSH_METHOD=wxpusher` 时必填，[获取地址](https://wxpusher.zjiecode.com/docs/#/?id=获取spt) | secrets   |
| `TELEGRAM_BOT_TOKEN`  <br>`TELEGRAM_CHAT_ID`   <br>`http_proxy`/`https_proxy`（可选）| 群组id以及机器人token                 | 当 `PUSH_METHOD=telegram` 时必填，[配置文档](https://www.nodeseek.com/post-22475-1) | secrets   |
| `SERVERCHAN_SPT`          | serverchan 的 SendKey               | 当 `PUSH_METHOD=serverchan` 时必填，[获取地址](https://sct.ftqq.com/sendkey) | secrets   |

**重要：除了READ_NUM配置在varables，其它的都配置在secrets里面的；需要推送`PUSH_METHOD`是必填的。**

### 视频教程

[![视频教程](https://github.com/user-attachments/assets/ec144869-3dbb-40fe-9bc5-f8bf1b5fce3c)](https://www.bilibili.com/video/BV1kJ6gY3En3/ "点击查看视频")


### 方法二： 服务器运行（docker部署）

**步骤1：** 创建 docker-compose.yml 文件

```yaml
services:
  wxread:
    image: ghcr.io/findmover/wxread:latest
    container_name: wxread
    restart: unless-stopped

    environment:
      TZ: "Asia/Shanghai"
      
      # 阅读次数（每次 30 秒），默认40为20分钟
      READ_NUM: 40
      
      # 微信读书 curl bash 命令（必需）
      # 使用 | 表示多行字符串，下一行开始粘贴完整的 curl 命令
      WXREAD_CURL_BASH: |
        curl 'https://weread.qq.com/web/book/read' \
        -H 'accept: application/json, text/plain, */*' \
        .....
      
      # 定时任务时间（Cron 表达式）
      CRON_SCHEDULE: "0 1 * * *"
      
      # 可选配置：推送方式 (pushplus/wxpusher/telegram/serverchan)
      PUSH_METHOD: ""
      # PushPlus 推送配置
      PUSHPLUS_TOKEN: ""
      # Telegram 推送配置
      TELEGRAM_BOT_TOKEN: ""
      TELEGRAM_CHAT_ID: ""
      # WxPusher 推送配置
      WXPUSHER_SPT: ""
      # ServerChan 推送配置
      SERVERCHAN_SPT: ""
```

**Cron 定时任务配置：**
- `0 1 * * *` - 每天凌晨1点（默认）
- `0 */6 * * *` - 每6小时
- `30 8 * * *` - 每天早上8:30

**步骤2：** 启动容器

```bash
docker-compose up -d
```

**步骤3：** 查看日志和测试

```bash
# 查看日志
docker-compose logs -f

# 测试运行
docker-compose exec wxread python /app/main.py
```

**Docker 说明：**
- 镜像地址：`ghcr.io/findmover/wxread:latest`
- 支持多架构：linux/amd64 和 linux/arm64
- 推送代码到仓库会自动构建最新镜像

***
## Attention 📢

1. **签到次数调整**：只需签到完成挑战赛可以将`num`次数从120调整为2，每次`num`为30秒，200即100分钟。
   
2. **解决阅读时间问题**：对于issue中提出的“阅读时间没有增加”，“增加时间与刷的时间不对等”建议保留`config.py`中的【data】字段，默认阅读三体，其它书籍自行测试。

3. **GitHub Action部署/本地部署**：主要配置config.py即可，Action部署使用环境变量，本地部署修改config.py里的阅读次数、headers、cookies即可。

4. **推送**：pushplus推送偶尔出问题，猜测是GitHub action环境问题，增加重试机制。并增加wxpusher的极简推送方式。


***
## 字段解释 🔍

| 字段 | 示例值 | 解释 |
| --- | --- | --- |
| `appId` | `"wbxxxxxxxxxxxxxxxxxxxxxxxx"` | 应用的唯一标识符。 |
| `b` | `"ce032b305a9bc1ce0b0dd2a"` | 书籍或章节的唯一标识符。 |
| `c` | `"0723244023c072b030ba601"` | 内容的唯一标识符，可能是页面或具体段落。 |
| `ci` | `60` | 章节或部分的索引。 |
| `co` | `336` | 内容的具体位置或页码。 |
| `sm` | `"[插图]威慑纪元61年，执剑人在一棵巨树"` | 当前阅读的内容描述或摘要。 |
| `pr` | `65` | 页码或段落索引。 |
| `rt` | `88` | 阅读时长或阅读进度。 |
| `ts` | `1727580815581` | 时间戳，表示请求发送的具体时间（毫秒级）。 |
| `rn` | `114` | 随机数或请求编号，用于标识唯一的请求。 |
| `sg` | `"bfdf7de2fe1673546ca079e2f02b79b937901ef789ed5ae16e7b43fb9e22e724"` | 安全签名，用于验证请求的合法性和完整性。 |
| `ct` | `1727580815` | 时间戳，表示请求发送的具体时间（秒级）。 |
| `ps` | `"xxxxxxxxxxxxxxxxxxxxxxxx"` | 用户标识符或会话标识符，用于追踪用户或会话。 |
| `pc` | `"xxxxxxxxxxxxxxxxxxxxxxxx"` | 设备标识符或客户端标识符，用于标识用户的设备或客户端。 |
| `s` | `"fadcb9de"` | 校验和或哈希值，用于验证请求数据的完整性。 |
