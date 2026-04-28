# 快速开始

这页用于帮助你在一台新机器上把脚本最小化跑起来。

## 1. 克隆仓库

```bash
git clone https://github.com/alanxie1999/YDX.git
cd YDX
```

## 2. 准备 Python 环境

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

如果遇到权限问题（如 `ERROR: Could not install packages due to EnvironmentPermissionError`），可以使用：

```bash
pip install -r requirements.txt --break-system-packages
```

安装完成后，可以通过以下命令验证依赖是否安装成功：

```bash
python3 -c "import telethon; print('Telethon version:', telethon.__version__)"
```

## 3. 准备通用配置

```bash
cp config/global_config.example.json config/global_config.json
```

编辑：

```text
config/global_config.json
```

至少确认这些内容：

- `groups.zq_group`
- `groups.zq_bot`
- 更新 token（如果你需要使用版本检查或自动更新）

## 4. 新建一个账号目录

下面以 `xu` 为例：

```bash
mkdir -p users/xu
cp users/_template/example_config.json users/xu/xu_config.json
cp users/_template/state.json.default users/xu/state.json
cp users/_template/presets.json.default users/xu/presets.json
```

## 5. 编辑账号配置

编辑：

```text
users/xu/xu_config.json
```

至少填这些字段：

- `telegram.api_id`
- `telegram.api_hash`
- `telegram.session_name`
- `telegram.user_id`
- `account.name`
- `zhuque.cookie`
- `zhuque.x_csrf`
- `admin_console`
- `notification.channels`
- `ai`

详细结构见 [配置说明](config.md)。

## 6. 放置 session

把该账号对应的 `.session` 文件放到：

```text
users/xu/
```

## 7. 启动脚本

```bash
python3 main_multiuser.py
```

启动成功后，通常会看到：

- 控制台显示账号启动成功
- 管理员入口收到“脚本启动成功”通知

## 8. 首次验证

启动成功后，建议依次测试：

```text
status
yss
help
```

如果管理员入口使用的是 Bot，则对应发送：

```text
/status
/yss
/help
```

## 常见启动问题

### 管理员入口未配置

当前版本要求 `admin_console` 必配。

如果缺失：

- 账号配置会加载失败
- 脚本不会进入正常运行态

### 使用了旧配置结构

管理员入口和通知渠道已经拆成不同配置块。

请直接使用新结构中的：

- `admin_console`
- `notification.channels`

### AI 配置是否必需？

当前版本使用简单跟随策略，**不需要配置 AI 密钥**。`ai` 配置块可以省略或留空。

