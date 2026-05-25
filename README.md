# Anyshare Share Link CLI

用于 Anyshare 公开分享链接的轻量 CLI 脚本，支持：
- 列出分享文件夹内容
- 上传本地文件到分享文件夹
- 下载分享文件夹中的文件

> 适合在 Linux 服务器上使用，无需 Anyshare 客户端。

## 依赖

- Python 3.8+
- requests

安装依赖：

```bash
python3 -m pip install requests
```

## 基本用法

脚本入口：`anyshare_cli.py`

```bash
python3 anyshare_cli.py --link "<分享链接>" <command> [options]
```

支持的命令：
- `list` 列出目录
- `upload` 上传文件
- `download` 下载文件

## 列出目录

```bash
python3 anyshare_cli.py --link "https://yunpan.ustb.edu.cn/link/AA..." list
```

列出子目录：

```bash
python3 anyshare_cli.py --link "https://yunpan.ustb.edu.cn/link/AA..." list --path "子目录/子子目录"
```

## 上传文件

```bash
python3 anyshare_cli.py --link "https://yunpan.ustb.edu.cn/link/AA..." \
  upload --file "/path/to/local.file"
```

上传到子目录：

```bash
python3 anyshare_cli.py --link "https://yunpan.ustb.edu.cn/link/AA..." \
  upload --file "/path/to/local.file" --path "子目录/子子目录"
```

可选参数：
- `--name` 远端文件名覆盖
- `--ondup` 文件重名处理策略（默认 1）

## 下载文件

按文件名下载：

```bash
python3 anyshare_cli.py --link "https://yunpan.ustb.edu.cn/link/AA..." \
  download --name "foo.zip"
```

按序号下载（序号来自 list 输出）：

```bash
python3 anyshare_cli.py --link "https://yunpan.ustb.edu.cn/link/AA..." \
  download --index 1
```

下载到指定路径：

```bash
python3 anyshare_cli.py --link "https://yunpan.ustb.edu.cn/link/AA..." \
  download --name "foo.zip" --out "/tmp/foo.zip"
```

可选参数：
- `--out` 输出文件或目录
- `--overwrite` 覆盖已有文件
- `--authtype` 下载鉴权类型（默认 "1"，如失败可尝试其他值）

## 常见问题

- 链接带密码：当前脚本不支持密码分享。
- 返回 401/403：分享链接可能过期、权限变化或鉴权类型不匹配。
- 文件重名冲突：可调整 `--ondup` 的值。

## 安全提示

- 不要把分享链接泄露给无关人员。
- 在脚本运行环境中避免输出或记录敏感信息。
