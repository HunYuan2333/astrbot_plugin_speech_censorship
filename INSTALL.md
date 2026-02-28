# 插件安装说明

## 【重要】最新修复（2026-02-28）

本次修复完全按照 **AstrBot 官方文档** 标准，解决了配置获取的问题。

### 改进说明

根据官方文档：[插件配置指南](https://docs.astrbot.app/dev/star/guides/plugin-config.html)

**原问题：** 使用了不存在的 `context.config_helper` 属性

**完整修复：**

1. 修改 `__init__` 方法签名，添加 `config: AstrBotConfig` 参数
2. 在 `__init__` 中将配置保存到 `self.config`
3. 在需要时通过 `self.config.get(key, default)` 直接访问

**修复前：**

```python
def __init__(self, context: Context):
    super().__init__(context)
    self.context = context
```

**修复后：**

```python
from astrbot.api import AstrBotConfig

def __init__(self, context: Context, config: AstrBotConfig):
    super().__init__(context)
    self.context = context
    self.config = config
```

这样插件在加载时，AstrBot 会自动注入配置对象，无需运行时获取。

## 安装步骤

### 方法 1：上传到 AstrBot（推荐）

1. **打包插件**

   ```bash
   # Windows PowerShell
   cd C:\Users\Administrator\Desktop
   Compress-Archive -Path speech-censorship\* -DestinationPath speech-censorship.zip -Force
   ```

2. **上传插件**
   - 登录 AstrBot WebUI
   - 进入"插件管理"页面
   - 点击"上传插件"
   - 选择 `speech-censorship.zip`
   - 等待安装完成

3. **配置插件**
   - 在插件列表中找到"群聊消息审核与自动禁言"
   - 点击"配置"
   - 设置以下必需项：
     - **LLM 提供商**：选择已配置的 LLM
   - 可选配置触发模式、禁言时长等参数

4. **测试功能**
   - 在群聊中发送：`/test_ban`
   - 验证是否被禁言 1 分钟

### 方法 2：手动复制

```bash
# 复制插件目录到 AstrBot
cp -r speech-censorship /path/to/AstrBot/data/plugins/

# 重启 AstrBot
```

## 配置文件说明

### metadata.yaml

插件元数据文件，包含：

- `name`: 插件唯一标识符
- `desc`: 插件描述
- `version`: 版本号
- `author`: 作者名称

### \_conf_schema.json

配置架构文件，定义了可在 WebUI 中编辑的配置项。

## 验证安装

### 查看日志

成功安装后，日志应显示：

```
[INFO] Plugin speech-censorship (1.0.0) by YourName: 群聊消息审核与自动禁言插件
[INFO] 群聊消息审核插件已加载
[INFO] 定时检测任务已启动（间隔: 60 秒，模式: hybrid）
[INFO] 群聊消息审核插件初始化完成
```

### 测试命令

在群聊中发送：

```
/test_ban
```

**预期结果：**

- ✅ 发送者被禁言 1 分钟
- ✅ 群内显示测试成功消息

**如果失败：**

- 检查 Bot 是否为群管理员
- 检查是否使用 QQ 平台（aiocqhttp）
- 查看日志中的错误详情

## 故障排查

### 问题：插件载入失败

**检查清单：**

1. metadata.yaml 格式是否正确
2. main.py 是否存在语法错误
3. AstrBot 版本是否兼容（建议 v4.18+）

### 问题：配置无法保存

**可能原因：**

- \_conf_schema.json 格式错误
- 配置值超出范围
- 权限问题

### 问题：LLM 调用失败

**检查清单：**

1. 是否在 AstrBot 中配置了 LLM 提供商
2. LLM API 密钥是否有效
3. 网络连接是否正常
4. 查看日志中的详细错误信息

## 更新插件

1. 删除旧版本插件
2. 按照安装步骤重新安装新版本
3. 重新配置插件参数

## 卸载插件

1. 在 AstrBot WebUI 的插件管理页面
2. 找到"群聊消息审核与自动禁言"
3. 点击"卸载"按钮
4. 确认删除

## 技术支持

如遇问题，请提供：

- AstrBot 版本
- 完整的错误日志
- 配置信息（隐藏敏感数据）
- 复现步骤
