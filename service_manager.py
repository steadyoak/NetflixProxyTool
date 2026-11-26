import os
import sys
import time
import re
import io
import zipfile
import shutil
import subprocess
import urllib.request
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

NETFLIX_HOSTS_TEMPLATE = """127.0.0.1 localhost localhost.localdomain
::1 localhost localhost.localdomain
!	/^(.+\\.)*netflix\\.com$
!	/^(.+\\.)*netflix\\.net$
!	/^(.+\\.)*nflximg\\.com$
!	/^(.+\\.)*nflximg\\.net$
!	/^(.+\\.)*nflxvideo\\.net$
!	/^(.+\\.)*nflxso\\.net$
!	/^(.+\\.)*nflxext\\.com$
"""

OFFICIAL_DOWNLOAD_URL = "https://downloads.sourceforge.net/project/acrylic/Acrylic/2.2.1/Acrylic-Portable.zip"

def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def get_system_proxy_url():
    """
    获取本机系统代理地址 (如 http://127.0.0.1:33210)
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enabled:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                if server:
                    if not server.startswith("http"):
                        server = f"http://{server}"
                    return server
    except Exception:
        pass

    try:
        auto_p = urllib.request.getproxies()
        if auto_p.get("https"):
            return auto_p.get("https")
        if auto_p.get("http"):
            return auto_p.get("http")
    except Exception:
        pass
    return None

def locate_acrylic_dir_strict(base_dir=None):
    """
    严格仅检测 exe 同级目录：
    1. exe 所在目录下的 Acrylic 文件夹 (base_dir/Acrylic)
    2. exe 自身所在目录 (base_dir)
    """
    if base_dir is None:
        base_dir = get_base_dir()

    c1 = os.path.join(base_dir, "Acrylic")
    if os.path.exists(os.path.join(c1, "AcrylicUI.exe")):
        return os.path.abspath(c1)

    if os.path.exists(os.path.join(base_dir, "AcrylicUI.exe")):
        return os.path.abspath(base_dir)

    return None

class ServiceManager:
    def __init__(self, acrylic_dir=None):
        self.base_dir = get_base_dir()
        if acrylic_dir is None:
            found = locate_acrylic_dir_strict(self.base_dir)
            if found:
                self.acrylic_dir = found
            else:
                self.acrylic_dir = os.path.join(self.base_dir, "Acrylic")
        else:
            self.acrylic_dir = os.path.abspath(acrylic_dir)

        self.update_paths()
        self.ensure_configuration_files()
        self.target_adapters = ["net3", "WLAN"]

    def update_paths(self):
        self.template_file = os.path.join(self.acrylic_dir, "AcrylicHostsTemplate.txt")
        self.hosts_file = os.path.join(self.acrylic_dir, "AcrylicHosts.txt")
        self.acrylic_ui_exe = os.path.join(self.acrylic_dir, "AcrylicUI.exe")
        self.acrylic_service_exe = os.path.join(self.acrylic_dir, "AcrylicService.exe")
        self.acrylic_console_exe = os.path.join(self.acrylic_dir, "AcrylicConsole.exe")
        self.config_ini_file = os.path.join(self.acrylic_dir, "AcrylicConfiguration.ini")

    def ensure_configuration_files(self):
        if not os.path.exists(self.acrylic_dir):
            return

        # 1. 确保 AcrylicConfiguration.ini 存在，配置默认上游 DNS，解决其他网站 404 / 无法解析问题
        if not os.path.exists(self.config_ini_file):
            same_dir_ini = os.path.join(self.base_dir, "AcrylicConfiguration.ini")
            if os.path.exists(same_dir_ini):
                try:
                    shutil.copy2(same_dir_ini, self.config_ini_file)
                except Exception:
                    pass
            if not os.path.exists(self.config_ini_file):
                with open(self.config_ini_file, "w", encoding="utf-8") as f:
                    f.write("[GlobalSection]\nPrimaryServerAddress=192.168.0.1\nPrimaryServerPort=53\nPrimaryServerProtocol=UDP\nSecondaryServerAddress=223.5.5.5\nSecondaryServerPort=53\nSecondaryServerProtocol=UDP\nTertiaryServerAddress=114.114.114.114\nTertiaryServerPort=53\nTertiaryServerProtocol=UDP\n")

        # 2. 确保 AcrylicHostsTemplate.txt 存在
        if not os.path.exists(self.template_file):
            with open(self.template_file, "w", encoding="utf-8") as f:
                f.write(NETFLIX_HOSTS_TEMPLATE)

        # 3. 确保 AcrylicHosts.txt 存在
        if not os.path.exists(self.hosts_file):
            with open(self.hosts_file, "w", encoding="utf-8") as f:
                f.write(NETFLIX_HOSTS_TEMPLATE.replace("!", "127.0.0.1"))

    def is_acrylic_installed(self):
        return os.path.exists(self.acrylic_ui_exe)

    def download_acrylic_zip(self, temp_zip_path, progress_callback=None):
        """
        使用多重稳健方式下载官方便携压缩包：
        1. 优先调用系统内置 curl.exe（自动挂载检测到的 VPN 代理）
        2. curl.exe 直连
        3. Python requests 请求
        """
        proxy_url = get_system_proxy_url()
        download_ok = False

        # 方式 1: Windows 原生 curl.exe 带代理
        if proxy_url:
            if progress_callback:
                progress_callback(f"正在通过本地代理 ({proxy_url}) 加速下载...")
            cmd = ["curl.exe", "-k", "-L", "-x", proxy_url, OFFICIAL_DOWNLOAD_URL, "-o", temp_zip_path]
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=0x08000000 if os.name == 'nt' else 0)
                if res.returncode == 0 and os.path.exists(temp_zip_path) and os.path.getsize(temp_zip_path) > 100000:
                    download_ok = True
            except Exception as e:
                if progress_callback:
                    progress_callback(f"代理下载提示: {e}")

        # 方式 2: Windows 原生 curl.exe 直连
        if not download_ok:
            if progress_callback:
                progress_callback("正在尝试直连官方节点下载...")
            cmd = ["curl.exe", "-k", "-L", OFFICIAL_DOWNLOAD_URL, "-o", temp_zip_path]
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=0x08000000 if os.name == 'nt' else 0)
                if res.returncode == 0 and os.path.exists(temp_zip_path) and os.path.getsize(temp_zip_path) > 100000:
                    download_ok = True
            except Exception:
                pass

        # 方式 3: Python requests
        if not download_ok:
            proxies_dict = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            try:
                resp = requests.get(OFFICIAL_DOWNLOAD_URL, headers=headers, proxies=proxies_dict, verify=False, timeout=25)
                if resp.status_code == 200 and len(resp.content) > 100000:
                    with open(temp_zip_path, "wb") as f:
                        f.write(resp.content)
                    download_ok = True
            except Exception:
                pass

        return download_ok

    def install_all_components(self, progress_callback=None):
        """
        一键完成：下载包 + 解压资源 + 初始化模板 + 注册安装 Windows 服务 + 启动服务
        """
        conflict, other_bin = self.check_service_conflict()
        if conflict:
            raise RuntimeError("Acyclic已经在别处启用了服务，请考虑手动卸载已有服务再使用本代理工具。")

        if progress_callback:
            progress_callback("开始准备安装 Acrylic 核心组件...")

        os.makedirs(self.acrylic_dir, exist_ok=True)
        temp_zip = os.path.join(self.base_dir, "temp_acrylic.zip")

        # 执行下载
        download_success = self.download_acrylic_zip(temp_zip, progress_callback=progress_callback)

        if not download_success:
            # 清理无效临时文件
            if os.path.exists(temp_zip):
                try:
                    os.remove(temp_zip)
                except Exception:
                    pass
            raise Exception("下载失败，需要启动网络工具来访问github。")

        # 解压
        if progress_callback:
            progress_callback(f"下载成功 ({os.path.getsize(temp_zip)//1024} KB)，正在解压至同级 Acrylic 目录...")

        try:
            with zipfile.ZipFile(temp_zip) as z:
                z.extractall(self.acrylic_dir)
        finally:
            if os.path.exists(temp_zip):
                try:
                    os.remove(temp_zip)
                except Exception:
                    pass

        if progress_callback:
            progress_callback("解压完成，正在配置 Netflix 规则模板...")

        self.update_paths()

        # 初始化 Netflix Hosts 模板与规则文件
        if not os.path.exists(self.template_file):
            with open(self.template_file, "w", encoding="utf-8") as f:
                f.write(NETFLIX_HOSTS_TEMPLATE)

        if not os.path.exists(self.hosts_file):
            with open(self.hosts_file, "w", encoding="utf-8") as f:
                f.write(NETFLIX_HOSTS_TEMPLATE.replace("!", "127.0.0.1"))

        if progress_callback:
            progress_callback("正在向 Windows 注册并安装 Acrylic 系统服务...")

        ok, msg = self.install_service()
        if not ok:
            raise Exception(f"服务注册失败: {msg}")

        if progress_callback:
            progress_callback("正在启动 Acrylic 本地 DNS 代理核心服务...")

        self.start_service()

        if progress_callback:
            progress_callback("Acrylic 服务安装并启动成功！")
        return True

    def get_service_bin_path(self):
        """
        获取 Windows 真实服务当前注册的二进制可执行文件路径
        """
        try:
            p = subprocess.run(
                ["sc.exe", "qc", "AcrylicDNSProxySvc"],
                capture_output=True,
                timeout=5,
                creationflags=0x08000000 if os.name == 'nt' else 0
            )
            stdout = p.stdout.decode("gbk", errors="ignore") if p.stdout else ""
            if p.returncode == 0 and stdout:
                for line in stdout.splitlines():
                    if "BINARY_PATH_NAME" in line.upper():
                        parts = line.split(":", 1)
                        if len(parts) > 1:
                            raw = parts[1].strip().strip('"').strip()
                            if raw:
                                return os.path.normcase(os.path.abspath(raw))
        except Exception:
            pass
        return None

    def check_service_conflict(self):
        """
        检测 Acrylic 服务是否已被系统其它路径注册占用：
        - 若服务未注册或注册路径即为本程序目录下的 AcrylicService.exe：返回 False, ""
        - 若服务已在系统注册且指向其它路径：返回 True, 冲突路径
        """
        current_bin = self.get_service_bin_path()
        if not current_bin:
            return False, ""
        my_bin = os.path.normcase(os.path.abspath(self.acrylic_service_exe))
        if current_bin != my_bin:
            return True, current_bin
        return False, ""

    def get_service_status(self):
        """
        获取 Windows 真实服务状态:
        'RUNNING', 'STOPPED', 'START_PENDING', 'STOP_PENDING', 'NOT_INSTALLED', 'UNKNOWN'
        """
        try:
            p = subprocess.run(
                ["sc.exe", "query", "AcrylicDNSProxySvc"],
                capture_output=True,
                timeout=5,
                creationflags=0x08000000 if os.name == 'nt' else 0
            )
            out = (p.stdout.decode("gbk", errors="ignore") if p.stdout else "").upper()
            if "1060" in out or "DOES NOT EXIST" in out or p.returncode != 0:
                return "NOT_INSTALLED"
            if "RUNNING" in out:
                return "RUNNING"
            if "STOPPED" in out:
                return "STOPPED"
            if "START_PENDING" in out:
                return "START_PENDING"
            if "STOP_PENDING" in out:
                return "STOP_PENDING"
        except Exception:
            pass
        return "UNKNOWN"

    def ensure_service_ready(self, progress_callback=None):
        """
        稳健性校验与自愈保证：
        1. 检查是否存在外部 Acrylic 服务冲突，若有则友好提示且不进行破坏性操作
        2. 检查 Acrylic 系统服务是否已注册安装，若未安装则自动注册安装
        3. 检查 Acrylic 服务是否处于 RUNNING 状态，若未运行则自动启动并轮询校验直至 RUNNING
        """
        conflict, other_bin = self.check_service_conflict()
        if conflict:
            msg = "Acyclic已经在别处启用了服务，请考虑手动卸载已有服务再使用本代理工具。"
            if progress_callback:
                progress_callback(f"⚠️ {msg}")
            return False, msg

        status = self.get_service_status()
        if status == "NOT_INSTALLED":
            if progress_callback:
                progress_callback("检测到 Acrylic 服务尚未在系统注册，正在自动注册安装服务...")
            ok, msg = self.install_service()
            time.sleep(1)
            status = self.get_service_status()
            if status == "NOT_INSTALLED":
                return False, f"自动安装 Acrylic 服务失败: {msg}"
            if progress_callback:
                progress_callback("✅ Acrylic 服务已成功在系统注册。")

        if status != "RUNNING":
            if progress_callback:
                progress_callback(f"Acrylic 服务当前为 [{status}] 状态，正在启动服务...")
            ok, msg = self.start_service()
            # 轮询等待确认进入 RUNNING
            for _ in range(6):
                time.sleep(0.8)
                status = self.get_service_status()
                if status == "RUNNING":
                    break

        if status != "RUNNING":
            return False, f"Acrylic 服务未能成功启动，当前状态: {status}。请确认已授予管理员权限。"

        if progress_callback:
            progress_callback("✅ Acrylic 核心服务校验通过，当前处于正常运行状态 (RUNNING)。")
        return True, "Acrylic 服务正常运行"

    def install_service(self):
        conflict, _ = self.check_service_conflict()
        if conflict:
            return False, "Acyclic已经在别处启用了服务，请考虑手动卸载已有服务再使用本代理工具。"

        # 1. 优先使用原生 AcrylicService.exe /INSTALL /SILENT
        if os.path.exists(self.acrylic_service_exe):
            try:
                subprocess.run(
                    [self.acrylic_service_exe, "/INSTALL", "/SILENT"],
                    cwd=self.acrylic_dir,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=0x08000000 if os.name == 'nt' else 0
                )
            except Exception:
                pass

        time.sleep(0.5)
        if self.get_service_status() != "NOT_INSTALLED":
            return True, "服务安装成功"

        # 2. 备用: AcrylicUI.exe InstallAcrylicService
        if os.path.exists(self.acrylic_ui_exe):
            try:
                subprocess.run(
                    [self.acrylic_ui_exe, "InstallAcrylicService"],
                    cwd=self.acrylic_dir,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=0x08000000 if os.name == 'nt' else 0
                )
            except Exception:
                pass

        time.sleep(1)
        if self.get_service_status() != "NOT_INSTALLED":
            return True, "服务安装成功"
        return False, "未能成功注册 Acrylic 系统服务，请确认拥有管理员权限"

    def restart_service(self):
        conflict, _ = self.check_service_conflict()
        if conflict:
            return False, "Acyclic已经在别处启用了服务，请考虑手动卸载已有服务再使用本代理工具。"

        status = self.get_service_status()
        # 若未在运行，直接执行启动即可
        if status != "RUNNING":
            return self.start_service()

        # 若正在运行，执行停用并重新启动
        try:
            subprocess.run(
                ["net.exe", "stop", "AcrylicDNSProxySvc"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=0x08000000 if os.name == 'nt' else 0
            )
        except Exception:
            pass

        time.sleep(0.5)

        try:
            subprocess.run(
                ["net.exe", "start", "AcrylicDNSProxySvc"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=0x08000000 if os.name == 'nt' else 0
            )
        except Exception:
            pass

        if self.get_service_status() != "RUNNING" and os.path.exists(self.acrylic_ui_exe):
            try:
                subprocess.run(
                    [self.acrylic_ui_exe, "RestartAcrylicService"],
                    cwd=self.acrylic_dir,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=0x08000000 if os.name == 'nt' else 0
                )
            except Exception:
                pass

        for _ in range(5):
            time.sleep(0.8)
            if self.get_service_status() == "RUNNING":
                return True, "Acrylic 服务已成功重新加载并保持运行"

        return False, f"Acrylic 服务重启后未处于运行状态: {self.get_service_status()}"

    def start_service(self):
        conflict, _ = self.check_service_conflict()
        if conflict:
            return False, "Acyclic已经在别处启用了服务，请考虑手动卸载已有服务再使用本代理工具。"

        status = self.get_service_status()
        if status == "RUNNING":
            return True, "Acrylic 服务已在运行"

        # 1. 优先使用 net.exe start AcrylicDNSProxySvc
        try:
            subprocess.run(
                ["net.exe", "start", "AcrylicDNSProxySvc"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=0x08000000 if os.name == 'nt' else 0
            )
        except Exception:
            pass

        # 2. 备用: AcrylicUI.exe StartAcrylicService
        if self.get_service_status() != "RUNNING" and os.path.exists(self.acrylic_ui_exe):
            try:
                subprocess.run(
                    [self.acrylic_ui_exe, "StartAcrylicService"],
                    cwd=self.acrylic_dir,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=0x08000000 if os.name == 'nt' else 0
                )
            except Exception:
                pass

        for _ in range(5):
            time.sleep(0.8)
            if self.get_service_status() == "RUNNING":
                return True, "Acrylic 服务已成功启动"

        return False, f"Acrylic 服务启动失败，当前状态: {self.get_service_status()}"

    def stop_service(self):
        try:
            subprocess.run(
                ["net.exe", "stop", "AcrylicDNSProxySvc"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=0x08000000 if os.name == 'nt' else 0
            )
        except Exception:
            pass

        if self.get_service_status() == "RUNNING" and os.path.exists(self.acrylic_ui_exe):
            try:
                subprocess.run(
                    [self.acrylic_ui_exe, "StopAcrylicService"],
                    cwd=self.acrylic_dir,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=0x08000000 if os.name == 'nt' else 0
                )
            except Exception:
                pass

        for _ in range(4):
            time.sleep(0.5)
            if self.get_service_status() != "RUNNING":
                return True, "Acrylic 服务已停止"

        return True, "Acrylic 服务已停止"

    def get_active_adapters(self):
        adapters = set(self.target_adapters)
        try:
            res = subprocess.run(
                ["netsh", "interface", "show", "interface"],
                capture_output=True,
                text=True,
                errors="ignore",
                timeout=5,
                creationflags=0x08000000 if os.name == 'nt' else 0
            )
            for line in res.stdout.splitlines():
                if "Connected" in line or "已连接" in line:
                    parts = line.split()
                    if len(parts) >= 4:
                        adapters.add(" ".join(parts[3:]))
        except Exception:
            pass
        return list(adapters)

    def get_current_ip(self):
        if not os.path.exists(self.hosts_file):
            return None
        try:
            with open(self.hosts_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    m = re.match(r"^(\d{1,3}(?:\.\d{1,3}){3})\s+", line)
                    if m:
                        ip = m.group(1)
                        if ip not in ("127.0.0.1", "0.0.0.0"):
                            return ip
        except Exception as e:
            print(f"读取当前 IP 异常: {e}")
        return None

    def update_hosts(self, new_ip):
        self.ensure_configuration_files()
        with open(self.template_file, "r", encoding="utf-8") as f:
            template_content = f.read()

        new_content = template_content.replace("!", new_ip.strip())

        with open(self.hosts_file, "w", encoding="utf-8") as f:
            f.write(new_content)
        return True

    def set_dns_127(self):
        adapters = self.get_active_adapters()
        results = []
        for iface in adapters:
            try:
                # 方式 1: 用户原始 bat 语法 (netsh interface ip set dns "name" static 127.0.0.1)
                cmd1 = ["netsh", "interface", "ip", "set", "dns", iface, "static", "127.0.0.1", "validate=no"]
                subprocess.run(cmd1, capture_output=True, text=True, timeout=5, creationflags=0x08000000 if os.name == 'nt' else 0)
                
                # 方式 2: 标准 name 键值语法
                cmd2 = ["netsh", "interface", "ip", "set", "dns", f"name={iface}", "source=static", "address=127.0.0.1", "validate=no"]
                subprocess.run(cmd2, capture_output=True, text=True, timeout=5, creationflags=0x08000000 if os.name == 'nt' else 0)
                
                results.append(f"适配器 [{iface}] 已锁定 127.0.0.1")
            except Exception as e:
                results.append(f"适配器 [{iface}] 设置异常: {e}")

        try:
            subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=5, creationflags=0x08000000 if os.name == 'nt' else 0)
            results.append("系统 DNS 缓存刷新成功 (/flushdns)")
        except Exception as e:
            results.append(f"刷新 DNS 缓存异常: {e}")

        return True, "\n".join(results)

    def set_dns_dhcp(self):
        adapters = self.get_active_adapters()
        results = []
        for iface in adapters:
            try:
                cmd1 = ["netsh", "interface", "ip", "set", "dns", iface, "dhcp"]
                subprocess.run(cmd1, capture_output=True, text=True, timeout=5, creationflags=0x08000000 if os.name == 'nt' else 0)

                cmd2 = ["netsh", "interface", "ip", "set", "dns", f"name={iface}", "source=dhcp"]
                subprocess.run(cmd2, capture_output=True, text=True, timeout=5, creationflags=0x08000000 if os.name == 'nt' else 0)

                results.append(f"适配器 [{iface}] 已恢复 DHCP")
            except Exception as e:
                results.append(f"适配器 [{iface}] 恢复异常: {e}")

        try:
            subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=5, creationflags=0x08000000 if os.name == 'nt' else 0)
            results.append("系统 DNS 缓存刷新成功 (/flushdns)")
        except Exception as e:
            results.append(f"刷新 DNS 缓存异常: {e}")

        return True, "\n".join(results)

    def check_is_dns_127(self):
        try:
            p = subprocess.run(
                ["netsh", "interface", "ip", "show", "dns"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=0x08000000 if os.name == 'nt' else 0
            )
            return "127.0.0.1" in p.stdout
        except Exception:
            return False

if __name__ == "__main__":
    mgr = ServiceManager()
    print("Found Acrylic Dir:", mgr.acrylic_dir)
    print("Is Installed in same directory:", mgr.is_acrylic_installed())
