# YdxbotV2

YdxbotV2 是一个基于 Telegram 的多账号自动化博弈机器人。

它面向长期在群里手动盯盘、手动押注的使用者，核心目标是把重复操作、状态查看、风险提醒和多账号管理收进一套统一流程。

## 主要功能

- 自动接收盘口消息与结算消息
- **简单跟随策略**：跟随上一手结果下注（开 1 押大，开 0 押小）
- **6 位交替检测**：检测到 10101/01010 纯交替模式时反向打破
- 支持多账号独立运行
- 支持连输告警、盈利暂停、炸号保护
- 支持暂停/恢复、预设管理、状态查询
- 支持管理员控制台与通知渠道分离
- 支持版本更新、回退与运行状态查看

## 策略说明

### 跟随模式

**默认策略**：简单跟随上一手开奖结果

| 上一手结果 | 下注方向 |
|-----------|---------|
| 1 (大)    | 押大    |
| 0 (小)    | 押小    |

### 交替打破

当检测到最近 5 手形成纯交替模式时，第 6 手反向打破：

| 最近 5 手 | 下注方向 | 说明 |
|---------|---------|------|
| 10101   | 小 (0)  | 反转为 101010 |
| 01010   | 大 (1)  | 反转为 010101 |

**退出交替**：当下注后出现两个相同方向（如 11 或 00），自动回到跟随策略。

## 适用场景

- 长期人工盯盘、重复手动下注
- 需要同时管理多个账号
- 希望把告警、暂停、恢复、统计查看集中起来
- 追求简单稳定的跟随策略

## 免责声明

本项目以开源形式提供，仅供学习、测试与技术研究使用。

使用者应自行判断其适用范围，并自行承担部署、运行、配置、更新及使用过程中产生的一切风险与责任。

项目维护者与贡献者不对任何直接或间接损失、封禁、数据异常、账户风险、平台风险或其他后果承担责任。

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/alanxie1999/YDX.git
cd YDX
```

### 2. 安装依赖

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

如果遇到权限问题，可以使用：

```bash
pip install -r requirements.txt --break-system-packages
```

### 3. 配置并启动

详细配置步骤请查看 [快速开始文档](docs/quick-start.md)

```bash
python3 main_multiuser.py
```

## 文档入口

详细文档请查看：

- 快速开始：[docs/quick-start.md](docs/quick-start.md)
- 配置说明：[docs/config.md](docs/config.md)
- 命令参考：[docs/commands.md](docs/commands.md)
- 功能说明：[docs/features.md](docs/features.md)

## 常见问题

### ModuleNotFoundError: No module named 'telethon'

这是因为依赖包未安装，请执行：

```bash
pip install -r requirements.txt
```

如果仍有问题，尝试：

```bash
pip install -r requirements.txt --break-system-packages
```

### 如何修改下注策略？

当前版本使用简单跟随策略，修改 `zq_multiuser.py` 中的预测逻辑部分即可自定义策略。

### 如何查看运行状态？

在管理员控制台发送 `/status` 命令查看当前账号状态、余额、历史等信息。
