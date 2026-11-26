<p align="center">
  <img src="app_icon.ico" width="96" height="96" alt="NetflixProxyTool Logo">
</p>

# NetflixProxyTool (网飞代理工具)

NetflixProxyTool 是一款专为 Windows 平台打造的高性能 SNI 反向代理与 Acrylic 本地 DNS 自动化管理工具。

## 核心特性
- **双引擎网络空间测绘**：支持奇安信鹰图 (Hunter) 与 FOFA 平台，自动化采集全球候选节点。
- **多地区自动切换**：支持台湾、香港、日本、美国、韩国、英国、加拿大、新加坡等常用区域。
- **本地节点池复用**：`server_pools.json` 本地持久化缓存，开机秒启，避免重复消耗测绘额度。
- **多线程并发测速**：内置并发 TLS/SNI 验证引擎，秒级优选最低延迟服务器。
- **Acrylic DNS 深度联动**：自动化托管 Acrylic 服务的安装、启动、Hosts 重定向与网卡 DNS 切换。
- **免 UAC 计划任务启动**：首次授权后自动注册 Windows 任务计划程序，后续启动无弹窗干扰。
- **原生暗黑美学界面**：自适应 Windows 系统深色主题，沉浸式标题栏与深色滚动条。

## 项目结构
- `main_gui.py`：主界面控制器与业务逻辑调度
- `service_manager.py`：Acrylic 系统服务与网卡 DNS 切换托管
- `verifier.py`：多线程并发 SNI 与 TLS 验证测速
- `hunter_scraper.py`：奇安信鹰图爬虫与多页会话采集
- `fofa_scraper.py`：FOFA 测绘平台爬虫与数据提取
- `app_icon.ico`：构建 EXE 时嵌入 PE 资源的高清应用图标
- `icon_b64.py`：内置 Base64 高清图标，确保窗口运行时零外部文件依赖
- `NetflixProxyTool.spec`：PyInstaller 自动化打包规范文件

## 运行与编译
```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 本地直接运行
python main_gui.py

# 3. 使用 PyInstaller 打包构建为单目录便携版
pyinstaller NetflixProxyTool.spec
```

## 开源许可
本项目遵循 MIT 开源许可协议。
