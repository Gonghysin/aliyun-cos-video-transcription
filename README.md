# Aliyun COS Video Transcription

使用阿里云 DashScope（通义/百炼）文件转录接口，将本地音频或视频批量转换为带时间戳的 JSON、TXT 和 SRT。脚本会先把媒体临时上传到私有腾讯云 COS，再把短期签名 URL 提交给 DashScope；任务完成后默认删除 COS 临时对象。

## 功能

- 支持单个文件、多个文件和目录递归处理
- 支持 `.aac`、`.flac`、`.m4a`、`.mkv`、`.mov`、`.mp3`、`.mp4`、`.wav`、`.webm`
- 支持并发任务、说话人区分和时间戳对齐
- 输出 JSON、可读 TXT、SRT 字幕和汇总报告
- 支持 dry-run，仅检查媒体和估算费用，不上传文件
- 支持 `.env`、环境变量和 macOS Keychain 三种凭据来源，不把密钥写入代码

## 环境要求

- macOS（可选，仅在未提供环境变量时使用内置 `security` 命令读取 Keychain）
- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/)
- `ffprobe`（通常随 FFmpeg 安装）
- 腾讯云 COS 私有 bucket
- 阿里云 DashScope 文件转录权限

依赖由 `uv` 临时提供，无需修改全局 Python 环境：

```bash
uv run --with cos-python-sdk-v5 --with dashscope --with requests --with python-dotenv \
  python scripts/transcribe_videos.py --help
```

## 配置凭据

推荐复制示例文件并编辑本地 `.env`：

```bash
cp .env.example .env
```

填写以下变量：

```dotenv
DASHSCOPE_API_KEY=你的 DashScope API Key
COS_SECRET_ID=你的腾讯云 COS SecretId
COS_SECRET_KEY=你的腾讯云 COS SecretKey
COS_BUCKET_NAME=你的私有 COS bucket 名
COS_REGION=ap-shanghai
```

`.env` 已被 Git 忽略，绝不要提交真实密钥。也可以直接在 shell 中导出同名环境变量；环境变量和 `.env` 优先于 macOS Keychain。

如果没有提供环境变量，脚本会回退到 macOS Keychain。默认 service 名称如下，账号默认为当前 macOS 用户名：

| 用途 | Keychain service |
| --- | --- |
| DashScope API Key | `DASHSCOPE_API_KEY` |
| COS SecretId | `COS_SECRET_ID` |
| COS SecretKey | `COS_SECRET_KEY` |

也可以通过环境变量指定已有的 service 名称，不要把密钥值粘贴到聊天、脚本或 Git：

```bash
export DASHSCOPE_KEYCHAIN_SERVICE="已有的 DashScope service 名称"
export COS_SECRET_ID_KEYCHAIN_SERVICE="已有的 COS SecretId service 名称"
export COS_SECRET_KEY_KEYCHAIN_SERVICE="已有的 COS SecretKey service 名称"
```

如果 Keychain 项目使用的账号不是当前用户名，可设置：

```bash
export KEYCHAIN_ACCOUNT="Keychain 项目的账号"
```

## 使用教程

### 1. 先做 dry-run

dry-run 不读取云端凭据、不上传媒体，只检查文件并生成费用估算。`--price-per-minute` 请填写你当前账单页面确认的单价；估算值不是最终账单。

```bash
export COS_BUCKET_NAME="你的私有 COS bucket 名"
export COS_REGION="你的 COS region，例如 ap-shanghai"

uv run --with cos-python-sdk-v5 --with dashscope --with requests --with python-dotenv \
  python scripts/transcribe_videos.py \
  ~/Downloads/meetings \
  --cos-bucket "$COS_BUCKET_NAME" \
  --cos-region "$COS_REGION" \
  --price-per-minute 0.01 \
  --output-dir ./transcripts \
  --dry-run
```

### 2. 提交转录任务

确认用户拥有媒体或已获授权，并确认上传和转录可能产生费用后，再执行正式任务：

```bash
uv run --with cos-python-sdk-v5 --with dashscope --with requests --with python-dotenv \
  python scripts/transcribe_videos.py \
  ~/Downloads/meeting-1.mp4 ~/Downloads/meeting-2.mp3 \
  --cos-bucket "$COS_BUCKET_NAME" \
  --cos-region "$COS_REGION" \
  --max-workers 2 \
  --output-dir ./transcripts
```

常用选项：

- `--max-workers 1-4`：并发数，默认 2；并发越高，同时上传和计费任务越多。
- `--model paraformer-v1`：DashScope 模型名。
- `--no-diarization`：关闭说话人区分。
- `--poll-interval 10`：任务轮询间隔秒数。
- `--keep-cos-source`：调试时保留 COS 临时对象；使用后应手动删除。

### 3. 查看输出

输出目录中每个源文件会生成：

- `文件名-路径摘要.json`：原始 provider 结果，含时间戳和说话人信息（若有）
- `文件名-路径摘要.txt`：带时间和说话人标签的可读文本
- `文件名-路径摘要.srt`：字幕文件
- `report.json`：所有任务的状态、Task ID、耗时、输出路径和费用字段

`report.json` 中的 `actual_cost` 在账单核对前为 `null`，不要把 `estimated_cost` 当作最终扣费。

## 故障处理

- 命令中断或终端断开后，先运行 `pgrep -fl transcribe_videos.py`，确认没有正在运行的任务，再决定是否重试。
- 如果已有 Task ID，不要重新上传媒体；优先使用原任务查询或重新下载结果。
- 如果无法判断任务是否已创建，先检查 COS 临时前缀和 DashScope 任务记录，避免重复提交和重复计费。
- 每个任务失败不会阻塞其他任务；查看 `report.json` 中对应 job 的 `error` 字段。

## 安全说明

- 本项目不包含任何真实 API Key、Secret、个人 bucket 名或本地绝对路径。
- 只对用户授权的媒体执行正式上传。
- 建议使用私有 COS bucket、最小权限凭据和短期签名 URL。
- 不要提交 `transcripts/`、`.env`、Keychain 导出或任何媒体文件。

## 社区

本项目认可并链接 [LINUX DO 社区](https://linux.do)。

## 许可证

MIT License
