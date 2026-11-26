import sys
import os
import time
import re
import json
import ctypes
import subprocess
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QLineEdit, QGroupBox,
    QMessageBox, QFrame, QGridLayout, QStackedWidget, QDialog,
    QComboBox, QTabWidget
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont, QIcon, QColor, QPixmap

from service_manager import ServiceManager
from verifier import verify_sni_server, batch_verify_sni
from fofa_scraper import FofaScraper
from hunter_scraper import HunterScraper, DEFAULT_HUNTER_QUERY

SUPPORTED_REGIONS = [
    {
        "code": "TW",
        "name": "台湾",
        "hunter": 'web.body="Backend not available"&&ip.province=="中国台湾"',
        "fofa": 'body="Backend not available" && region="TW"'
    },
    {
        "code": "HK",
        "name": "香港",
        "hunter": 'web.body="Backend not available"&&ip.province=="中国香港"',
        "fofa": 'body="Backend not available" && region="HK"'
    },
    {
        "code": "JP",
        "name": "日本",
        "hunter": 'web.body="Backend not available"&&ip.country=="日本"',
        "fofa": 'body="Backend not available" && country="JP"'
    },
    {
        "code": "US",
        "name": "美国",
        "hunter": 'web.body="Backend not available"&&ip.country=="美国"',
        "fofa": 'body="Backend not available" && country="US"'
    },
    {
        "code": "KR",
        "name": "韩国",
        "hunter": 'web.body="Backend not available"&&ip.country=="韩国"',
        "fofa": 'body="Backend not available" && country="KR"'
    },
    {
        "code": "GB",
        "name": "英国",
        "hunter": 'web.body="Backend not available"&&ip.country=="英国"',
        "fofa": 'body="Backend not available" && country="GB"'
    },
    {
        "code": "CA",
        "name": "加拿大",
        "hunter": 'web.body="Backend not available"&&ip.country=="加拿大"',
        "fofa": 'body="Backend not available" && country="CA"'
    },
    {
        "code": "SG",
        "name": "新加坡",
        "hunter": 'web.body="Backend not available"&&ip.country=="新加坡"',
        "fofa": 'body="Backend not available" && country="SG"'
    }
]

def get_app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

class WorkerThread(QThread):
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(dict)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, action_type, **kwargs):
        super().__init__()
        self.action_type = action_type
        self.kwargs = kwargs
        self.source = kwargs.get("source", "hunter")
        self.region_code = kwargs.get("region_code", "TW")
        self.service_mgr = ServiceManager()
        self.fofa_scraper = FofaScraper()
        self.hunter_scraper = HunterScraper()

        self.hunter_scraper.set_region(self.region_code)
        self.fofa_scraper.set_region(self.region_code)

        for r in SUPPORTED_REGIONS:
            if r["code"] == self.region_code:
                self.hunter_scraper.set_query(r["hunter"])
                self.fofa_scraper.set_query(r["fofa"])
                break

    def log(self, text):
        self.log_signal.emit(text)

    def run(self):
        try:
            if self.action_type == "install_all_components":
                self.handle_install_all_components()
            elif self.action_type == "start_or_toggle":
                self.handle_start_or_toggle()
            elif self.action_type == "stop_service":
                self.handle_stop_service()
            elif self.action_type == "force_change":
                self.handle_force_change()
            elif self.action_type == "login":
                self.handle_login()
            elif self.action_type == "manual_ip":
                self.handle_manual_ip(self.kwargs.get("ip"))
        except Exception as e:
            self.log(f"执行异常: {str(e)}")
            self.finished_signal.emit(False, str(e))

    def handle_install_all_components(self):
        conflict, other_bin = self.service_mgr.check_service_conflict()
        if conflict:
            msg = "Acyclic已经在别处启用了服务，请考虑手动卸载已有服务再使用本代理工具。"
            self.log(f"❌ {msg}")
            self.finished_signal.emit(False, msg)
            return

        self.log("开始安装 Acrylic 核心组件...")
        try:
            self.service_mgr.install_all_components(progress_callback=self.log)
            self.finished_signal.emit(True, "组件安装成功！")
        except Exception as e:
            self.log(f"安装失败: {e}")
            self.finished_signal.emit(False, str(e))

    def handle_stop_service(self):
        self.log("正在停止反代服务...")
        ok, msg = self.service_mgr.stop_service()
        self.log("Acrylic 服务已停止")
        
        self.log("正在切换系统 DNS 为动态获取 (DHCP)...")
        self.service_mgr.set_dns_dhcp()
        self.log("已恢复系统为动态 DNS (DHCP)，DNS 缓存已刷新。")

        self.status_signal.emit({
            "is_running": False,
            "ip": self.service_mgr.get_current_ip() or "未配置",
            "ip_status": "服务已停用",
            "dns_status": "动态获取 (DHCP)"
        })
        self.finished_signal.emit(True, "服务已成功关闭，已恢复动态 DNS。")

    def handle_start_or_toggle(self):
        self.log("正在检查 Acrylic 系统核心服务状态...")
        ok, msg = self.service_mgr.ensure_service_ready(progress_callback=self.log)
        if not ok:
            self.log(f"❌ {msg}")
            self.finished_signal.emit(False, f"服务未就绪: {msg}")
            return

        current_ip = self.service_mgr.get_current_ip()
        self.log(f"正在检查上次使用的服务器 IP: {current_ip or '无'}...")

        usable_ip = None
        if current_ip:
            self.log(f"正在验证当前 IP [{current_ip}] 的 SNI 可用性...")
            ok, latency, msg = verify_sni_server(current_ip, timeout=3.0)
            if ok:
                self.log(f"当前 IP [{current_ip}] 验证通过！{msg}")
                usable_ip = current_ip
            else:
                self.log(f"当前 IP [{current_ip}] 已失效 ({msg})，准备重新获取...")

        if not usable_ip:
            source_label = "鹰图" if self.source == "hunter" else "FOFA"
            self.log(f"当前无可用服务器，正在临时将系统 DNS 重置为动态获取 (DHCP)，以确保能正常访问【{source_label}】与互联网...")
            self.service_mgr.set_dns_dhcp()
            time.sleep(0.5)
            self.status_signal.emit({
                "is_running": False,
                "dns_status": "动态获取 (DHCP)"
            })
            usable_ip = self.discover_and_verify_new_ip(source=self.source, exclude_ip=current_ip)

        if not usable_ip:
            source_label = "鹰图" if self.source == "hunter" else "FOFA"
            self.log(f"❌ 未能获取到任何可用的 SNI 反代服务器！请检查【{source_label}】登录或手动指定备用 IP。")
            self.finished_signal.emit(False, f"未找到可用服务器，请尝试登录 {source_label} 或切换数据源。")
            return

        self.apply_server_and_start(usable_ip)

    def handle_force_change(self):
        source_label = "鹰图" if self.source == "hunter" else "FOFA"
        self.log(f"⚡ 收到指令：强制更换服务器 (使用数据源: 【{source_label}】)...")
        self.log("正在重置系统 DNS 为动态获取 (DHCP)，以确保能够正常访问测绘平台与网络...")
        self.service_mgr.set_dns_dhcp()
        time.sleep(0.5)
        self.status_signal.emit({
            "is_running": False,
            "dns_status": "动态获取 (DHCP)"
        })

        ok, msg = self.service_mgr.ensure_service_ready(progress_callback=self.log)
        if not ok:
            self.log(f"❌ {msg}")
            self.finished_signal.emit(False, f"服务未就绪: {msg}")
            return
        current_ip = self.service_mgr.get_current_ip()
        usable_ip = self.discover_and_verify_new_ip(source=self.source, exclude_ip=current_ip)
        if not usable_ip:
            self.log("❌ 强制更换失败，未能找到新的可用服务器！系统保持动态 DNS。")
            self.finished_signal.emit(False, "未找到可用新服务器。")
            return
        self.apply_server_and_start(usable_ip)

    def discover_and_verify_new_ip(self, source="hunter", exclude_ip=None, max_pages=10):
        scraper = self.hunter_scraper if source == "hunter" else self.fofa_scraper
        source_name = "奇安信鹰图" if source == "hunter" else "FOFA"
        region_name = "台湾"
        for r in SUPPORTED_REGIONS:
            if r["code"] == self.region_code:
                region_name = r["name"]
                break

        # 1. 优先检查本地候选池中的备用服务器（避免重复消耗查询积分）
        available_cached = scraper.get_available_pool(exclude_ip=exclude_ip)
        if available_cached:
            self.log(f"📦 命中本地候选池（剩余 {len(available_cached)} 个备用服务器），正在并发测速（免耗积分/无需开浏览器）...")
            valid_results, failed_results = batch_verify_sni(available_cached, timeout=3.5, max_workers=10)

            # 将失效的标记为 dead，以后不再重复测
            for dead_ip, _, _ in failed_results:
                scraper.mark_ip_dead(dead_ip)

            self.log(f"本地备用池测速完成：可用 {len(valid_results)} 个，失效 {len(failed_results)} 个。")
            if valid_results:
                best_ip, best_latency, best_msg = valid_results[0]
                self.log(f"🏆 在本地备用池中成功选出最佳服务器: [{best_ip}] (延迟: {best_latency}ms)")
                return best_ip
            else:
                self.log(f"⚠️ 本地备用池中的所有服务器均已验证失效。准备重新从【{source_name}】检索并扩充...")
                scraper.clear_pool()

        # 2. 如果本地池为空或全部失效，从平台检索新页面（不打印具体检索语法）
        self.log(f"🌐 正在从【{source_name}】检索多页候选服务器 ({region_name}) 并扩充本地池...")
        candidate_ips = scraper.get_candidate_ips(page=1, log_callback=self.log)

        if not candidate_ips:
            self.log(f"未获取到候选 IP，请确保【{source_name}】账号已登录或已配置 Key/Cookie。")
            return None

        # 过滤掉当前需要排除的 IP
        test_ips = [ip for ip in candidate_ips if ip != exclude_ip]
        if not test_ips:
            test_ips = candidate_ips

        self.log(f"正在对新获取的 {len(test_ips)} 个候选服务器进行并发 SNI 验证测速...")
        valid_results, failed_results = batch_verify_sni(test_ips, timeout=3.5, max_workers=10)
        for dead_ip, _, _ in failed_results:
            scraper.mark_ip_dead(dead_ip)

        self.log(f"测速完成：可用 {len(valid_results)} 个，不可用 {len(failed_results)} 个。")
        if valid_results:
            best_ip, best_latency, best_msg = valid_results[0]
            self.log(f"🏆 成功选定最佳可用服务器: [{best_ip}] (延迟: {best_latency}ms)")
            return best_ip

        self.log(f"❌ 本次从【{source_name}】获取的候选服务器均未通过验证。")
        return None

    def apply_server_and_start(self, ip):
        self.log("正在验证 Acrylic 系统核心服务状态...")
        ok, msg = self.service_mgr.ensure_service_ready(progress_callback=self.log)
        if not ok:
            self.log(f"❌ 服务准备失败: {msg}")
            self.finished_signal.emit(False, msg)
            return

        self.log(f"正在更新 AcrylicHosts.txt，设置反代目标为 [{ip}]...")
        self.service_mgr.update_hosts(ip)
        self.log("Hosts 规则更新成功！")

        self.log("正在重新加载并确认 Acrylic DNS 核心服务运行...")
        ok, msg = self.service_mgr.restart_service()
        if not ok:
            self.log(f"❌ 服务启动校验失败: {msg}")
            self.finished_signal.emit(False, f"反代配置已更新，但 Acrylic 服务未处于运行状态: {msg}")
            return
        self.log("✅ Acrylic DNS 服务已重新加载并在运行中 (RUNNING)。")

        self.log("正在设置系统 DNS 为 127.0.0.1 (本地解析)...")
        self.service_mgr.set_dns_127()
        self.log("系统 DNS 已指向 127.0.0.1，DNS 缓存已刷新。")

        self.status_signal.emit({
            "is_running": True,
            "ip": ip,
            "ip_status": "正常可用",
            "dns_status": "127.0.0.1 (已锁定)"
        })
        self.finished_signal.emit(True, f"反代服务已成功开启！当前 IP: {ip}")

    def handle_login(self):
        source_name = "奇安信鹰图" if self.source == "hunter" else "FOFA"
        self.log(f"正在将系统 DNS 临时重置为动态获取 (DHCP)，确保能够正常打开【{source_name}】登录页面...")
        self.service_mgr.set_dns_dhcp()
        time.sleep(0.5)
        self.status_signal.emit({
            "is_running": False,
            "dns_status": "动态获取 (DHCP)"
        })
        self.log(f"即将调起 Edge 浏览器进行【{source_name}】登录与多页采集...")
        scraper = self.hunter_scraper if self.source == "hunter" else self.fofa_scraper
        ips = scraper.interactive_login_and_fetch(max_pages=10, log_callback=self.log)
        if not ips:
            self.log(f"未能从【{source_name}】登录会话中获取到有效服务器，请确认是否成功登录且具备查询权限。")
            self.finished_signal.emit(False, f"{source_name} 登录完成但未提取到数据。")
            return

        self.log(f"✅ 登录与采集完成！共获取到 {len(ips)} 个服务器 IP，正在立即进行并发 SNI 验证测速...")
        valid_results, failed_results = batch_verify_sni(ips, timeout=3.5, max_workers=10)
        for dead_ip, _, _ in failed_results:
            scraper.mark_ip_dead(dead_ip)

        self.log(f"并发测速完成：可用 {len(valid_results)} 个，不可用 {len(failed_results)} 个。")
        if valid_results:
            best_ip, best_latency, best_msg = valid_results[0]
            self.log(f"🏆 成功选定最佳可用服务器: [{best_ip}] (延迟: {best_latency}ms)")
            self.apply_server_and_start(best_ip)
        else:
            self.log("⚠️ 本次获取到的候选服务器均未通过 Netflix SNI 验证。IP 已保存在本地候选池。")
            self.finished_signal.emit(False, "候选服务器验证未通过，请尝试更换其他数据源。")

    def handle_manual_ip(self, ip):
        if not ip or not FofaScraper.is_valid_real_ipv4(ip):
            self.finished_signal.emit(False, "输入的 IP 地址格式不正确！")
            return
        self.log(f"正在对用户手动输入的 IP [{ip}] 进行 SNI 可用性验证...")
        ok, latency, msg = verify_sni_server(ip, timeout=3.5)
        if not ok:
            self.log(f"⚠️ 警告: 该 IP 验证未通过 ({msg})，但仍按用户要求应用。")
        else:
            self.log(f"✅ 该 IP 验证通过！{msg}")

        self.apply_server_and_start(ip)


def apply_windows_dark_title_bar(window_hwnd):
    """
    使 Windows 窗口标题栏自适应系统暗色/亮色主题（在暗色模式下显示沉浸式暗色标题栏）。
    在 Windows 10 (1809+) 及 Windows 11 上无缝生效。
    """
    if sys.platform != "win32":
        return
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        )
        apps_use_light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        is_dark = (apps_use_light == 0)
    except Exception:
        is_dark = True  # 软件界面本身为暗色，无法获取时默认暗色标题栏

    try:
        # DWMWA_USE_IMMERSIVE_DARK_MODE: Win11/Win10 20H1+ = 20, 早期 Win10 1809-1909 = 19
        val = ctypes.c_int(1 if is_dark else 0)
        hwnd = int(window_hwnd)
        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(val), ctypes.sizeof(val)
        )
        if res != 0:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 19, ctypes.byref(val), ctypes.sizeof(val)
            )
    except Exception:
        pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("网飞代理工具 - Netflix Proxy Tool")
        self.resize(800, 620)
        
        self.is_running = False
        self.setup_icon()
        self.service_mgr = ServiceManager()
        self.fofa_scraper = FofaScraper()
        self.hunter_scraper = HunterScraper()
        self.worker = None

        # 初始化时根据已保存地区设置爬虫检索目标
        self.update_scrapers_for_region(self.get_current_region_code())

        self.init_ui()

    def showEvent(self, event):
        super().showEvent(event)
        hwnd = int(self.winId())
        apply_windows_dark_title_bar(hwnd)
        if hasattr(self, 'app_icon') and not self.app_icon.isNull():
            self.setWindowIcon(self.app_icon)
            self.bind_win32_icon(hwnd)
        # 窗体初始化就绪后，将自身切为当前前台窗口，防止被其它窗口遮挡
        self.bring_to_front()
        QTimer.singleShot(150, self.bring_to_front)

    def bring_to_front(self):
        try:
            self.show()
            self.raise_()
            self.activateWindow()

            hwnd = int(self.winId())
            if hwnd:
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32

                SWP_NOMOVE = 0x0002
                SWP_NOSIZE = 0x0001
                SWP_SHOWWINDOW = 0x0040
                HWND_TOPMOST = -1
                HWND_NOTOPMOST = -2

                # 瞬时置顶激活打破前台锁定，再恢复正常非锁定（避免一直遮挡用户后续窗口）
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
                user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)

                # 关联当前线程输入并切为 Foreground
                fg_hwnd = user32.GetForegroundWindow()
                cur_thread = kernel32.GetCurrentThreadId()
                if fg_hwnd and fg_hwnd != hwnd:
                    fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
                    if fg_thread and fg_thread != cur_thread:
                        user32.AttachThreadInput(cur_thread, fg_thread, True)
                        user32.SetForegroundWindow(hwnd)
                        user32.BringWindowToTop(hwnd)
                        user32.SetFocus(hwnd)
                        user32.AttachThreadInput(cur_thread, fg_thread, False)
                    else:
                        user32.SetForegroundWindow(hwnd)
                        user32.BringWindowToTop(hwnd)
                else:
                    user32.SetForegroundWindow(hwnd)
                    user32.BringWindowToTop(hwnd)
        except Exception:
            pass

    def bind_win32_icon(self, hwnd):
        try:
            WM_SETICON = 0x0080
            # 1. 优先使用 Win32 原生 LoadImageW 直接从 _internal 或本地 physical ico 加载原生 HICON
            if hasattr(self, 'physical_ico_path') and self.physical_ico_path and os.path.exists(self.physical_ico_path):
                IMAGE_ICON = 1
                LR_LOADFROMFILE = 0x00000010
                h_small = ctypes.windll.user32.LoadImageW(None, self.physical_ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
                h_big = ctypes.windll.user32.LoadImageW(None, self.physical_ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
                if h_small:
                    self._h_small = h_small
                    ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 0, int(h_small))
                if h_big:
                    self._h_big = h_big
                    ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 1, int(h_big))
                return

            # 2. 备选方案：从 base_pixmap 转换
            if hasattr(self, 'base_pixmap') and not self.base_pixmap.isNull():
                from PyQt5.QtWinExtras import QtWin
                p16 = self.base_pixmap.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                p32 = self.base_pixmap.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._h_small = QtWin.toHICON(p16)
                self._h_big = QtWin.toHICON(p32)
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 0, int(self._h_small))
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 1, int(self._h_big))
        except Exception:
            pass

    def setup_icon(self):
        # 1. 优先定位 _internal 依赖目录或本地 physical ico 路径
        self.physical_ico_path = None
        candidate_paths = [
            os.path.join(getattr(sys, '_MEIPASS', ''), "app_icon.ico"),
            os.path.join(get_app_dir(), "_internal", "app_icon.ico"),
            os.path.join(get_app_dir(), "app_icon.ico"),
            os.path.join(os.path.dirname(__file__), "app_icon.ico"),
        ]
        for cp in candidate_paths:
            if cp and os.path.exists(cp):
                self.physical_ico_path = cp
                break

        if self.physical_ico_path:
            try:
                self.base_pixmap = QPixmap(self.physical_ico_path)
                self.app_icon = QIcon(self.physical_ico_path)
                self.setWindowIcon(self.app_icon)
                QApplication.setWindowIcon(self.app_icon)
                return
            except Exception:
                pass

        # 2. 备选方案：从内置高清 Base64 生成多分辨率 QIcon
        try:
            from icon_b64 import APP_ICON_PNG_B64, APP_ICON_B64
            from PyQt5.QtCore import QByteArray
            raw_data = APP_ICON_PNG_B64 or APP_ICON_B64
            if raw_data:
                pix = QPixmap()
                if pix.loadFromData(QByteArray.fromBase64(raw_data.encode('ascii'))) and not pix.isNull():
                    self.base_pixmap = pix
                    self.app_icon = QIcon()
                    for s in [16, 20, 24, 32, 40, 48, 64, 128, 256]:
                        self.app_icon.addPixmap(self.base_pixmap.scaled(s, s, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    self.setWindowIcon(self.app_icon)
                    QApplication.setWindowIcon(self.app_icon)
                    return
        except Exception:
            pass

    def get_current_source(self):
        if hasattr(self, 'combo_source'):
            return self.combo_source.currentData() or "hunter"
        return self.hunter_scraper.config.get("search_source", "hunter")

    def get_current_region_code(self):
        if hasattr(self, 'combo_region'):
            return self.combo_region.currentData() or "TW"
        return self.hunter_scraper.config.get("target_region", "TW")

    def get_current_region_name(self):
        code = self.get_current_region_code()
        for r in SUPPORTED_REGIONS:
            if r["code"] == code:
                return r["name"]
        return "台湾"

    def update_scrapers_for_region(self, region_code):
        self.hunter_scraper.set_region(region_code)
        self.fofa_scraper.set_region(region_code)
        for r in SUPPORTED_REGIONS:
            if r["code"] == region_code:
                self.hunter_scraper.set_query(r["hunter"])
                self.fofa_scraper.set_query(r["fofa"])
                break

    def init_ui(self):
        # 整体高对比暗色主题
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e24;
            }
            QWidget {
                color: #e0e0e0;
                font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #3d3d4a;
                border-radius: 8px;
                margin-top: 12px;
                padding-top: 16px;
                padding-bottom: 12px;
                padding-left: 14px;
                padding-right: 14px;
                font-weight: bold;
                background-color: #262630;
            }
            QGroupBox::title {
                subcontrol-origin: border;
                subcontrol-position: top left;
                left: 14px;
                top: 0px;
                padding: 0 6px;
                color: #4da6ff;
                font-size: 13px;
                font-weight: bold;
                background-color: #262630;
            }
            QScrollBar:vertical {
                border: none;
                background-color: #141418;
                width: 10px;
                margin: 0px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background-color: #383848;
                min-height: 25px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #505068;
            }
            QScrollBar::handle:vertical:pressed {
                background-color: #606080;
            }
            QScrollBar::sub-line:vertical, QScrollBar::add-line:vertical {
                border: none;
                background: none;
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }
            QScrollBar:horizontal {
                border: none;
                background-color: #141418;
                height: 10px;
                margin: 0px;
                border-radius: 5px;
            }
            QScrollBar::handle:horizontal {
                background-color: #383848;
                min-width: 25px;
                border-radius: 5px;
            }
            QScrollBar::handle:horizontal:hover {
                background-color: #505068;
            }
            QScrollBar::sub-line:horizontal, QScrollBar::add-line:horizontal {
                border: none;
                background: none;
                width: 0px;
            }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: none;
            }
            QPushButton {
                background-color: #383848;
                border: 1px solid #505068;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: bold;
                color: #ffffff;
            }
            QPushButton:hover {
                background-color: #4a4a60;
                border-color: #6a6a88;
            }
            QPushButton:pressed {
                background-color: #2a2a38;
            }
            QLineEdit {
                background-color: #1a1a20;
                border: 1px solid #444458;
                border-radius: 4px;
                padding: 6px 10px;
                color: #ffffff;
            }
            QLineEdit:focus {
                border-color: #4da6ff;
            }
            QTextEdit {
                background-color: #141418;
                border: 1px solid #333340;
                border-radius: 6px;
                color: #00ffcc;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 13px;
                padding: 8px;
            }
            QTabWidget::pane {
                border: 1px solid #3d3d4a;
                background-color: #22222a;
                border-radius: 4px;
            }
            QTabBar::tab {
                background: #2d2d38;
                color: #aaaaaa;
                padding: 8px 20px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: #3d3d50;
                color: #4da6ff;
                font-weight: bold;
            }
            QDialog, QMessageBox {
                background-color: #22222a !important;
            }
            QMessageBox QLabel {
                color: #ffffff !important;
                font-size: 14px;
                background-color: transparent !important;
            }
            QMessageBox QPushButton {
                background-color: #383848 !important;
                border: 1px solid #55556a !important;
                border-radius: 4px;
                padding: 6px 20px;
                color: #ffffff !important;
                font-weight: bold;
                min-width: 70px;
            }
            QMessageBox QPushButton:hover {
                background-color: #4a4a60 !important;
            }
        """)

        self.stacked_widget = QStackedWidget()
        self.setCentralWidget(self.stacked_widget)

        # 页面 0: 仅单个按钮的精简初始化页面
        self.setup_page = self.create_simple_install_page()
        self.stacked_widget.addWidget(self.setup_page)

        # 页面 1: 主运行控制仪表盘
        self.main_dashboard_page = self.create_dashboard_page()
        self.stacked_widget.addWidget(self.main_dashboard_page)

        # 检测同级是否有 Acrylic
        conflict, conflict_path = self.service_mgr.check_service_conflict()
        if self.service_mgr.is_acrylic_installed():
            self.stacked_widget.setCurrentIndex(1)
            self.refresh_display_status()
            if conflict:
                self.append_log("⚠️ 提示：Acyclic已经在别处启用了服务，请考虑手动卸载已有服务再使用本代理工具。")
            else:
                svc_st = self.service_mgr.get_service_status()
                if svc_st == "RUNNING":
                    self.append_log("Acrylic 核心组件已就绪，后台服务正常运行中。")
                else:
                    self.append_log(f"检测到 Acrylic 本地组件 (服务状态: {svc_st})。点击【开启服务】将自动为您完成服务启动与配置。")
        else:
            self.stacked_widget.setCurrentIndex(0)
            if conflict:
                self.append_install_log("⚠️ 提示：Acyclic已经在别处启用了服务，请考虑手动卸载已有服务再使用本代理工具。")

    # ---------------- 页面 0: 精简安装组件页面 ----------------
    def create_simple_install_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(50, 50, 50, 50)
        layout.setSpacing(20)
        layout.addStretch()

        title_lbl = QLabel("未检测到 Acrylic 核心组件")
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setFont(QFont("Segoe UI", 18, QFont.Bold))
        title_lbl.setStyleSheet("color: #f39c12;")
        layout.addWidget(title_lbl)

        sub_lbl = QLabel("当前同级目录下缺少 Acrylic 组件。点击下方按钮一键完成下载与系统服务配置。")
        sub_lbl.setAlignment(Qt.AlignCenter)
        sub_lbl.setStyleSheet("color: #b0b0b8; font-size: 13px;")
        layout.addWidget(sub_lbl)

        self.btn_install_components = QPushButton("安装组件")
        self.btn_install_components.setFixedHeight(54)
        self.btn_install_components.setFont(QFont("Segoe UI", 14, QFont.Bold))
        self.btn_install_components.setStyleSheet("""
            QPushButton {
                background-color: #27ae60;
                border: 1px solid #2ecc71;
                border-radius: 8px;
                color: #ffffff;
            }
            QPushButton:hover {
                background-color: #2ecc71;
            }
        """)
        self.btn_install_components.clicked.connect(self.on_click_install_components)
        layout.addWidget(self.btn_install_components)

        self.error_banner = QLabel("❌ 下载失败，需要启动网络工具来访问github。")
        self.error_banner.setAlignment(Qt.AlignCenter)
        self.error_banner.setStyleSheet("""
            background-color: #381a1a;
            color: #ff6b6b;
            border: 1px solid #ff4d4f;
            border-radius: 6px;
            font-size: 14px;
            font-weight: bold;
            padding: 12px;
        """)
        self.error_banner.hide()
        layout.addWidget(self.error_banner)

        self.install_log = QTextEdit()
        self.install_log.setReadOnly(True)
        self.install_log.setFixedHeight(140)
        self.install_log.setStyleSheet("""
            background-color: #101014;
            border: 1px solid #2d2d38;
            color: #00ffcc;
            font-family: 'Consolas', 'Segoe UI', monospace;
            font-size: 13px;
        """)
        layout.addWidget(self.install_log)

        layout.addStretch()
        return page

    def append_install_log(self, text):
        if not re.match(r"^\[\d{2}:\d{2}:\d{2}\]", text):
            now_str = time.strftime("%H:%M:%S")
            text = f"[{now_str}] {text}"
        self.install_log.append(text)

    def on_click_install_components(self):
        self.error_banner.hide()
        self.btn_install_components.setEnabled(False)
        self.btn_install_components.setText("正在安装组件，请稍候...")
        self.append_install_log("正在检索 Acrylic 核心资源并进行安装...")

        self.worker = WorkerThread("install_all_components")
        self.worker.log_signal.connect(self.append_install_log)
        self.worker.finished_signal.connect(self.on_install_components_finished)
        self.worker.start()

    def on_install_components_finished(self, success, message):
        if success:
            self.error_banner.hide()
            self.append_install_log("✅ 组件安装成功，正在进入控制面板...")
            self.service_mgr.update_paths()
            self.stacked_widget.setCurrentIndex(1)
            self.refresh_display_status()
            self.append_log("Acrylic 核心组件已就绪，服务正常运行中。")
        else:
            self.btn_install_components.setEnabled(True)
            self.btn_install_components.setText("安装组件")
            self.error_banner.show()

            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("网络提示")
            msg_box.setText("下载失败，需要启动网络工具来访问github。")
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setStyleSheet("""
                QMessageBox {
                    background-color: #22222a;
                    border: 1px solid #55556a;
                }
                QLabel {
                    color: #ffffff !important;
                    font-size: 14px;
                    font-weight: bold;
                    background-color: transparent;
                }
                QPushButton {
                    background-color: #383848;
                    border: 1px solid #55556a;
                    border-radius: 4px;
                    padding: 6px 20px;
                    color: #ffffff;
                    font-weight: bold;
                    min-width: 70px;
                }
                QPushButton:hover {
                    background-color: #4a4a60;
                }
            """)
            msg_box.exec_()

    # ---------------- 页面 1: 主运行控制仪表盘 ----------------
    def create_dashboard_page(self):
        page = QWidget()
        main_layout = QVBoxLayout(page)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(16, 16, 16, 16)

        # 1. 状态仪表盘面板 (带破边边框设计，标题破开边框线)
        status_group = QGroupBox("系统与反代状态")
        grid = QGridLayout(status_group)
        grid.setContentsMargins(16, 20, 16, 14)
        grid.setVerticalSpacing(12)
        grid.setHorizontalSpacing(30)

        # 统一左侧提示文本固定宽度为 120px，保证后面的状态说明严格左对齐
        PROMPT_WIDTH = 120

        # ====== 左侧两个: 反代运行状态 & Acrylic 服务状态 ======
        lbl_p_run = QLabel("反代运行状态:")
        lbl_p_run.setFixedWidth(PROMPT_WIDTH)
        lbl_p_run.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(lbl_p_run, 0, 0)

        self.lbl_run_status = QLabel("检测中...")
        self.lbl_run_status.setFont(QFont("Segoe UI", 12, QFont.Bold))
        self.lbl_run_status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.lbl_run_status, 0, 1)

        lbl_p_svc = QLabel("Acrylic 服务状态:")
        lbl_p_svc.setFixedWidth(PROMPT_WIDTH)
        lbl_p_svc.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(lbl_p_svc, 1, 0)

        self.lbl_service_status = QLabel("检测中...")
        self.lbl_service_status.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.lbl_service_status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.lbl_service_status, 1, 1)

        # ====== 右侧两个: 当前反代服务器 & 系统 DNS 状态 ======
        lbl_p_ip = QLabel("当前反代服务器:")
        lbl_p_ip.setFixedWidth(PROMPT_WIDTH)
        lbl_p_ip.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(lbl_p_ip, 0, 2)

        self.lbl_current_ip = QLabel("读取中...")
        self.lbl_current_ip.setFont(QFont("Consolas", 12, QFont.Bold))
        self.lbl_current_ip.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.lbl_current_ip, 0, 3)

        lbl_p_dns = QLabel("系统 DNS 状态:")
        lbl_p_dns.setFixedWidth(PROMPT_WIDTH)
        lbl_p_dns.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(lbl_p_dns, 1, 2)

        self.lbl_dns_status = QLabel("检测中...")
        self.lbl_dns_status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.lbl_dns_status, 1, 3)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        main_layout.addWidget(status_group)

        # 2. 核心主操作区域
        # 大按钮: 开启服务 / 关闭服务
        self.btn_toggle = QPushButton("▶ 开启服务")
        self.btn_toggle.setFixedHeight(48)
        self.btn_toggle.setFont(QFont("Segoe UI", 13, QFont.Bold))
        self.btn_toggle.clicked.connect(self.on_click_toggle)
        main_layout.addWidget(self.btn_toggle)

        # 下方小按钮行: 换服务器、恢复DNS、更多 (包装在容器 QWidget 中，点击更多时隐藏，腾出空间给更多面板)
        self.sub_btn_widget = QWidget()
        sub_btn_layout = QHBoxLayout(self.sub_btn_widget)
        sub_btn_layout.setContentsMargins(0, 0, 0, 0)
        sub_btn_layout.setSpacing(10)

        self.btn_force_change = QPushButton("🔄 换服务器")
        self.btn_force_change.setFixedHeight(36)
        self.btn_force_change.setStyleSheet("""
            QPushButton {
                background-color: #1f538d;
                border: 1px solid #2b78d4;
                font-size: 13px;
                font-weight: bold;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #2b78d4;
            }
        """)
        self.btn_force_change.clicked.connect(self.on_click_force_change)
        sub_btn_layout.addWidget(self.btn_force_change, 1)

        self.btn_restore_dns = QPushButton("🌐 恢复DNS")
        self.btn_restore_dns.setFixedHeight(36)
        self.btn_restore_dns.setStyleSheet("""
            QPushButton {
                background-color: #383848;
                border: 1px solid #505068;
                font-size: 13px;
                font-weight: bold;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #4a4a60;
            }
        """)
        self.btn_restore_dns.clicked.connect(self.on_click_reset_dhcp)
        sub_btn_layout.addWidget(self.btn_restore_dns, 1)

        self.btn_more = QPushButton("⚙️ 更多...")
        self.btn_more.setFixedHeight(36)
        self.btn_more.setStyleSheet("""
            QPushButton {
                background-color: #2c3e50;
                border: 1px solid #34495e;
                font-size: 13px;
                font-weight: bold;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #34495e;
            }
        """)
        self.btn_more.clicked.connect(self.on_click_toggle_more)
        sub_btn_layout.addWidget(self.btn_more, 1)

        main_layout.addWidget(self.sub_btn_widget)

        # 3. 展开式“更多”面板 (带破边边框设计)
        self.more_panel = QGroupBox("更多高级设置与工具")
        self.more_panel.setStyleSheet("""
            QGroupBox {
                border: 1px solid #4da6ff;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 14px;
                padding-bottom: 8px;
                padding-left: 10px;
                padding-right: 10px;
                background-color: #23232c;
            }
            QGroupBox::title {
                subcontrol-origin: border;
                subcontrol-position: top left;
                left: 14px;
                top: 0px;
                padding: 0 6px;
                color: #4da6ff;
                font-size: 12px;
                font-weight: bold;
                background-color: #23232c;
            }
            QPushButton {
                min-height: 36px;
                max-height: 36px;
                font-size: 13px;
                font-weight: bold;
                border-radius: 5px;
                padding: 0px 10px;
            }
            QLineEdit {
                min-height: 36px;
                max-height: 36px;
                font-size: 13px;
                border-radius: 5px;
                padding: 0px 8px;
                background-color: #1a1a22;
                border: 1px solid #444455;
                color: #ffffff;
            }
            QComboBox {
                min-height: 36px;
                max-height: 36px;
                font-size: 13px;
                font-weight: bold;
                border-radius: 5px;
                padding: 0px 8px;
                background-color: #2b2b38;
                border: 1px solid #4da6ff;
                color: #ffffff;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #22222a;
                color: #ffffff;
                selection-background-color: #1f538d;
            }
        """)
        more_vbox = QVBoxLayout(self.more_panel)
        more_vbox.setSpacing(10)
        more_vbox.setContentsMargins(10, 10, 10, 8)

        # 更多面板内部：左右两列排版布局
        cols_layout = QHBoxLayout()
        cols_layout.setSpacing(24)
        cols_layout.setContentsMargins(4, 4, 4, 4)

        # ====== 左列 ======
        # 测绘网站（无冒号）、网飞地区（无冒号）、两个按钮（配置凭据、Edge登录）
        left_col = QVBoxLayout()
        left_col.setSpacing(10)

        # 左列第 1 行: 测绘网站
        row_left1 = QHBoxLayout()
        row_left1.setSpacing(10)
        lbl_src_title = QLabel("测绘网站")
        lbl_src_title.setFixedWidth(64)
        lbl_src_title.setFont(QFont("Segoe UI", 10, QFont.Bold))
        lbl_src_title.setStyleSheet("color: #4da6ff;")
        row_left1.addWidget(lbl_src_title)

        self.combo_source = QComboBox()
        self.combo_source.setFixedHeight(36)
        self.combo_source.addItem("🦅 奇安信鹰图", "hunter")
        self.combo_source.addItem("🔍 FOFA 平台", "fofa")
        saved_source = self.hunter_scraper.config.get("search_source", "hunter")
        idx_src = self.combo_source.findData(saved_source)
        self.combo_source.setCurrentIndex(idx_src if idx_src >= 0 else 0)
        self.combo_source.currentIndexChanged.connect(self.on_source_changed)
        row_left1.addWidget(self.combo_source, 1)
        left_col.addLayout(row_left1)

        # 左列第 2 行: 网飞地区
        row_left2 = QHBoxLayout()
        row_left2.setSpacing(10)
        lbl_reg_title = QLabel("网飞地区")
        lbl_reg_title.setFixedWidth(64)
        lbl_reg_title.setFont(QFont("Segoe UI", 10, QFont.Bold))
        lbl_reg_title.setStyleSheet("color: #4da6ff;")
        row_left2.addWidget(lbl_reg_title)

        self.combo_region = QComboBox()
        self.combo_region.setFixedHeight(36)
        for r in SUPPORTED_REGIONS:
            self.combo_region.addItem(r["name"], r["code"])
        saved_region = self.hunter_scraper.config.get("target_region", "TW")
        idx_reg = self.combo_region.findData(saved_region)
        self.combo_region.setCurrentIndex(idx_reg if idx_reg >= 0 else 0)
        self.combo_region.currentIndexChanged.connect(self.on_region_changed)
        row_left2.addWidget(self.combo_region, 1)
        left_col.addLayout(row_left2)

        # 左列第 3 行: 两个按钮 (配置凭据、Edge登录)
        row_left3 = QHBoxLayout()
        row_left3.setSpacing(10)

        self.btn_config_keys = QPushButton("🔑 配置凭据")
        self.btn_config_keys.setFixedHeight(36)
        self.btn_config_keys.clicked.connect(self.on_click_config_keys)
        row_left3.addWidget(self.btn_config_keys, 1)

        self.btn_login = QPushButton("Edge 登录鹰图" if self.get_current_source() == "hunter" else "Edge 登录 FOFA")
        self.btn_login.setFixedHeight(36)
        self.btn_login.clicked.connect(self.on_click_login)
        row_left3.addWidget(self.btn_login, 1)
        left_col.addLayout(row_left3)

        cols_layout.addLayout(left_col, 1)

        # ====== 右列 ======
        # 没有左侧提示文字，文本框内写“手动填写IP”，下面是“测试并应用”
        right_col = QVBoxLayout()
        right_col.setSpacing(10)

        self.txt_manual_ip = QLineEdit()
        self.txt_manual_ip.setFixedHeight(36)
        self.txt_manual_ip.setPlaceholderText("手动填写IP")
        right_col.addWidget(self.txt_manual_ip)

        self.btn_apply_manual = QPushButton("测试并应用")
        self.btn_apply_manual.setFixedHeight(36)
        self.btn_apply_manual.setStyleSheet("""
            QPushButton {
                background-color: #1f538d;
                border: 1px solid #2b78d4;
                color: #ffffff;
            }
            QPushButton:hover {
                background-color: #2b78d4;
            }
        """)
        self.btn_apply_manual.clicked.connect(self.on_click_apply_manual)
        right_col.addWidget(self.btn_apply_manual)

        # 下方信息行: 左侧 1.0.0版本，右侧 Github 仓库按钮
        row_about = QHBoxLayout()
        row_about.setContentsMargins(0, 4, 0, 0)
        row_about.setSpacing(10)

        lbl_version = QLabel("1.0.0版本")
        lbl_version.setStyleSheet("color: #888899; font-size: 12px; font-weight: bold;")
        lbl_version.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        row_about.addWidget(lbl_version)

        self.btn_github = QPushButton("🐙 GitHub 仓库")
        self.btn_github.setFixedHeight(32)
        self.btn_github.setStyleSheet("""
            QPushButton {
                background-color: #2b2b36;
                border: 1px solid #454555;
                color: #4da6ff;
                font-size: 12px;
                font-weight: bold;
                border-radius: 4px;
                padding: 0 10px;
            }
            QPushButton:hover {
                background-color: #38384a;
                border-color: #4da6ff;
                color: #ffffff;
            }
        """)
        self.btn_github.clicked.connect(self.on_click_github)
        row_about.addWidget(self.btn_github)

        right_col.addLayout(row_about)

        right_col.addStretch()
        cols_layout.addLayout(right_col, 1)

        more_vbox.addLayout(cols_layout)

        # ====== 底部居中收起按钮 ======
        row3 = QHBoxLayout()
        row3.addStretch()
        self.btn_collapse = QPushButton("🔼 收起")
        self.btn_collapse.setFixedHeight(36)
        self.btn_collapse.setFixedWidth(120)
        self.btn_collapse.setStyleSheet("""
            QPushButton {
                background-color: #383848;
                border: 1px solid #55556a;
                color: #ffffff;
                font-size: 13px;
                font-weight: bold;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #4a4a60;
                color: #ffffff;
            }
        """)
        self.btn_collapse.clicked.connect(self.on_click_collapse_more)
        row3.addWidget(self.btn_collapse)
        row3.addStretch()
        more_vbox.addLayout(row3)

        # 默认收起隐藏
        self.more_panel.hide()
        main_layout.addWidget(self.more_panel)

        # 4. 实时日志区域 (说明文本位于边线以下)
        log_group = QGroupBox("实时运行日志")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(12, 18, 12, 10)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        log_layout.addWidget(self.log_box)

        log_btn_layout = QHBoxLayout()
        log_btn_layout.addStretch()
        btn_clear_log = QPushButton("清空日志")
        btn_clear_log.setFixedWidth(90)
        btn_clear_log.clicked.connect(self.log_box.clear)
        log_btn_layout.addWidget(btn_clear_log)
        log_layout.addLayout(log_btn_layout)

        main_layout.addWidget(log_group, 1)

        source_name = "奇安信鹰图" if self.get_current_source() == "hunter" else "FOFA 平台"
        region_name = self.get_current_region_name()
        self.append_log(f"系统就绪。当前测绘源: 【{source_name}】 | 服务器地区: 【{region_name}】。点击【开启服务】即可自动验证并启动。")
        scraper = self.hunter_scraper if self.get_current_source() == "hunter" else self.fofa_scraper
        cached_count = len(scraper.get_cached_pool())
        dead_count = len(scraper.get_dead_ips())
        self.append_log(f"[本地节点池] 已从 exe 同级目录 server_pools.json 加载 {region_name}: 候选 {cached_count} 个, 已跳过 {dead_count} 个 (优先复用，不消耗配额)")
        return page

    def on_click_toggle_more(self):
        self.sub_btn_widget.hide()
        self.more_panel.show()

    def on_click_collapse_more(self):
        self.more_panel.hide()
        self.sub_btn_widget.show()

    def on_click_github(self):
        import webbrowser
        webbrowser.open("https://github.com/steadyoak/NetflixProxyTool")

    def on_source_changed(self, index):
        source = self.combo_source.currentData()
        if source == "hunter":
            self.btn_login.setText("Edge 登录鹰图")
            self.append_log("已切换数据源为: 【奇安信鹰图】")
        else:
            self.btn_login.setText("Edge 登录 FOFA")
            self.append_log("已切换数据源为: 【FOFA 平台】")

        # 持久化保存用户偏好
        self.hunter_scraper.config["search_source"] = source
        self.hunter_scraper.save_config()

        scraper = self.hunter_scraper if source == "hunter" else self.fofa_scraper
        region_name = self.get_current_region_name()
        cached = scraper.get_cached_pool()
        dead = scraper.get_dead_ips()
        self.append_log(f"[本地节点池] 已从 exe 同级目录 server_pools.json 读取 {region_name}: 候选 {len(cached)} 个, 已跳过 {len(dead)} 个")

    def on_region_changed(self, index):
        region_code = self.combo_region.currentData()
        self.update_scrapers_for_region(region_code)
        region_name = self.get_current_region_name()
        self.append_log(f"已切换服务器地区为: 【{region_name}】")

        self.hunter_scraper.config["target_region"] = region_code
        self.hunter_scraper.save_config()

        scraper = self.hunter_scraper if self.get_current_source() == "hunter" else self.fofa_scraper
        cached = scraper.get_cached_pool()
        dead = scraper.get_dead_ips()
        self.append_log(f"[本地节点池] 已从 exe 同级目录 server_pools.json 读取 {region_name}: 候选 {len(cached)} 个, 已跳过 {len(dead)} 个")

    def append_log(self, text):
        if not re.match(r"^\[\d{2}:\d{2}:\d{2}\]", text):
            now_str = time.strftime("%H:%M:%S")
            text = f"[{now_str}] {text}"
        self.log_box.append(text)

    def refresh_display_status(self):
        current_ip = self.service_mgr.get_current_ip() or "未配置"
        self.lbl_current_ip.setText(current_ip)

        conflict, conflict_path = self.service_mgr.check_service_conflict()
        svc_status = self.service_mgr.get_service_status()
        if conflict:
            self.lbl_service_status.setText("🔴 别处已启用服务")
            self.lbl_service_status.setStyleSheet("color: #e74c3c; font-weight: bold;")
        elif svc_status == "RUNNING":
            self.lbl_service_status.setText("🟢 RUNNING (正常运行)")
            self.lbl_service_status.setStyleSheet("color: #4cd964; font-weight: bold;")
        elif svc_status == "STOPPED":
            self.lbl_service_status.setText("🟡 STOPPED (已安装未运行)")
            self.lbl_service_status.setStyleSheet("color: #f1c40f; font-weight: bold;")
        elif svc_status == "NOT_INSTALLED":
            self.lbl_service_status.setText("🔴 NOT_INSTALLED (未注册)")
            self.lbl_service_status.setStyleSheet("color: #e74c3c; font-weight: bold;")
        else:
            self.lbl_service_status.setText(f"⚪ {svc_status}")
            self.lbl_service_status.setStyleSheet("color: #aaaaaa; font-weight: bold;")

        is_dns_127 = self.service_mgr.check_is_dns_127()
        is_running = getattr(self, "is_running", False)
        if is_running or is_dns_127:
            if svc_status == "RUNNING":
                self.lbl_run_status.setText("🟢 正常运行 (反代已生效)")
                self.lbl_run_status.setStyleSheet("color: #4cd964; font-weight: bold;")
            else:
                self.lbl_run_status.setText(f"⚠️ 异常 (服务状态: {svc_status})")
                self.lbl_run_status.setStyleSheet("color: #e67e22; font-weight: bold;")

            self.lbl_dns_status.setText("127.0.0.1 (本地解析)")
            self.btn_toggle.setText("⏹ 关闭服务")
            self.btn_toggle.setStyleSheet("""
                QPushButton {
                    background-color: #c0392b;
                    border-color: #e74c3c;
                }
                QPushButton:hover {
                    background-color: #e74c3c;
                }
            """)
            self.is_running = True
        else:
            self.lbl_run_status.setText("⚪ 未开启 (动态DNS)")
            self.lbl_run_status.setStyleSheet("color: #aaaaaa; font-weight: bold;")
            self.lbl_dns_status.setText("动态获取 (DHCP)")
            self.btn_toggle.setText("▶ 开启服务")
            self.btn_toggle.setStyleSheet("""
                QPushButton {
                    background-color: #27ae60;
                    border-color: #2ecc71;
                }
                QPushButton:hover {
                    background-color: #2ecc71;
                }
            """)
            self.is_running = False

    def set_buttons_enabled(self, enabled):
        self.btn_toggle.setEnabled(enabled)
        self.btn_force_change.setEnabled(enabled)
        self.btn_restore_dns.setEnabled(enabled)
        self.btn_more.setEnabled(enabled)
        if hasattr(self, 'btn_login'):
            self.btn_login.setEnabled(enabled)
        if hasattr(self, 'btn_config_keys'):
            self.btn_config_keys.setEnabled(enabled)
        if hasattr(self, 'btn_apply_manual'):
            self.btn_apply_manual.setEnabled(enabled)

    def on_click_toggle(self):
        self.set_buttons_enabled(False)
        source = self.get_current_source()
        region = self.get_current_region_code()
        if self.is_running:
            self.worker = WorkerThread("stop_service", source=source, region_code=region)
        else:
            self.worker = WorkerThread("start_or_toggle", source=source, region_code=region)

        self.worker.log_signal.connect(self.append_log)
        self.worker.status_signal.connect(self.on_worker_status)
        self.worker.finished_signal.connect(self.on_worker_finished)
        self.worker.start()

    def on_click_force_change(self):
        self.set_buttons_enabled(False)
        source = self.get_current_source()
        region = self.get_current_region_code()
        self.worker = WorkerThread("force_change", source=source, region_code=region)
        self.worker.log_signal.connect(self.append_log)
        self.worker.status_signal.connect(self.on_worker_status)
        self.worker.finished_signal.connect(self.on_worker_finished)
        self.worker.start()

    def on_click_login(self):
        self.set_buttons_enabled(False)
        source = self.get_current_source()
        region = self.get_current_region_code()
        self.worker = WorkerThread("login", source=source, region_code=region)
        self.worker.log_signal.connect(self.append_log)
        self.worker.status_signal.connect(self.on_worker_status)
        self.worker.finished_signal.connect(self.on_worker_finished)
        self.worker.start()

    def on_click_config_keys(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("测绘平台凭据与 API 配置")
        dialog.resize(580, 420)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #22222a;
            }
            QLabel {
                color: #e0e0e0;
                font-size: 13px;
            }
            QLineEdit, QTextEdit {
                background-color: #181820;
                border: 1px solid #444458;
                border-radius: 4px;
                color: #ffffff;
                font-family: Consolas, monospace;
                padding: 6px;
            }
            QLineEdit:focus, QTextEdit:focus {
                border-color: #4da6ff;
            }
            QPushButton {
                background-color: #383848;
                border: 1px solid #55556a;
                border-radius: 4px;
                padding: 6px 18px;
                color: #ffffff;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #4a4a60;
            }
        """)

        d_layout = QVBoxLayout(dialog)
        d_layout.setSpacing(12)

        tabs = QTabWidget()

        # Tab 1: 鹰图 (Hunter)
        tab_hunter = QWidget()
        h_layout = QVBoxLayout(tab_hunter)
        h_layout.setSpacing(10)

        h_layout.addWidget(QLabel("<b>奇安信鹰图 (Hunter) API-KEY:</b> (每日赠送免费积分，免打开浏览器)"))
        txt_hunter_key = QLineEdit()
        txt_hunter_key.setText(self.hunter_scraper.config.get("hunter_api_key", ""))
        txt_hunter_key.setPlaceholderText("在 hunter.qianxin.com 个人中心 -> API-KEY 获取")
        h_layout.addWidget(txt_hunter_key)

        h_layout.addWidget(QLabel("<b>鹰图 Cookie / Token:</b> (可选，从浏览器抓取的 Cookie 粘贴于此)"))
        txt_hunter_cookie = QTextEdit()
        txt_hunter_cookie.setFixedHeight(110)
        txt_hunter_cookie.setPlaceholderText("例如: token=xxx; 或整段 HTTP 请求头 Cookie")
        if self.hunter_scraper.config.get("hunter_cookies"):
            txt_hunter_cookie.setPlainText("; ".join([f"{k}={v}" for k, v in self.hunter_scraper.config["hunter_cookies"].items()]))
        h_layout.addWidget(txt_hunter_cookie)

        tabs.addTab(tab_hunter, "🦅 奇安信鹰图")

        # Tab 2: FOFA
        tab_fofa = QWidget()
        f_layout = QVBoxLayout(tab_fofa)
        f_layout.setSpacing(10)

        f_layout.addWidget(QLabel("<b>FOFA 账号 Email:</b>"))
        txt_fofa_email = QLineEdit()
        txt_fofa_email.setText(self.fofa_scraper.config.get("fofa_email", ""))
        f_layout.addWidget(txt_fofa_email)

        f_layout.addWidget(QLabel("<b>FOFA API Key:</b>"))
        txt_fofa_key = QLineEdit()
        txt_fofa_key.setText(self.fofa_scraper.config.get("fofa_key", ""))
        f_layout.addWidget(txt_fofa_key)

        f_layout.addWidget(QLabel("<b>FOFA Cookie:</b> (支持粘贴包含 fofa_token 的整行 Cookie)"))
        txt_fofa_cookie = QTextEdit()
        txt_fofa_cookie.setFixedHeight(90)
        if self.fofa_scraper.config.get("cookies"):
            txt_fofa_cookie.setPlainText("; ".join([f"{k}={v}" for k, v in self.fofa_scraper.config["cookies"].items()]))
        f_layout.addWidget(txt_fofa_cookie)

        tabs.addTab(tab_fofa, "🔍 FOFA 测绘")

        d_layout.addWidget(tabs)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(dialog.reject)
        btn_save = QPushButton("💾 保存配置")
        btn_save.setStyleSheet("background-color: #27ae60; border-color: #2ecc71;")

        def do_save():
            # 保存鹰图
            h_key = txt_hunter_key.text().strip()
            self.hunter_scraper.config["hunter_api_key"] = h_key
            h_raw_c = txt_hunter_cookie.toPlainText().strip()
            if h_raw_c:
                self.hunter_scraper.import_cookie_string(h_raw_c)
            self.hunter_scraper.save_config()

            # 保存 FOFA
            self.fofa_scraper.config["fofa_email"] = txt_fofa_email.text().strip()
            self.fofa_scraper.config["fofa_key"] = txt_fofa_key.text().strip()
            f_raw_c = txt_fofa_cookie.toPlainText().strip()
            if f_raw_c:
                self.fofa_scraper.import_cookie_string(f_raw_c)
            self.fofa_scraper.save_config()

            dialog.accept()
            self.append_log("已成功保存 API 与 Cookie 配置！")
            QMessageBox.information(self, "成功", "测绘平台凭据与配置已保存！")

        btn_save.clicked.connect(do_save)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        d_layout.addLayout(btn_row)

        dialog.exec_()

    def on_click_apply_manual(self):
        ip = self.txt_manual_ip.text().strip()
        if not ip:
            QMessageBox.warning(self, "提示", "请输入有效的服务器 IP！")
            return
        self.set_buttons_enabled(False)
        self.worker = WorkerThread("manual_ip", ip=ip)
        self.worker.log_signal.connect(self.append_log)
        self.worker.status_signal.connect(self.on_worker_status)
        self.worker.finished_signal.connect(self.on_worker_finished)
        self.worker.start()

    def on_click_reset_dhcp(self):
        self.set_buttons_enabled(False)
        self.worker = WorkerThread("stop_service")
        self.worker.log_signal.connect(self.append_log)
        self.worker.status_signal.connect(self.on_worker_status)
        self.worker.finished_signal.connect(self.on_worker_finished)
        self.worker.start()

    def on_worker_status(self, data):
        if "is_running" in data:
            self.is_running = data["is_running"]
        self.refresh_display_status()

    def on_worker_finished(self, success, message):
        self.set_buttons_enabled(True)
        if success:
            if self.worker and self.worker.action_type in ("start_or_toggle", "force_change", "manual_ip"):
                self.is_running = True
            elif self.worker and self.worker.action_type == "stop_service":
                self.is_running = False
        self.refresh_display_status()
        if not success:
            QMessageBox.warning(self, "操作提示", message)

def check_and_elevate_with_task_scheduler():
    """
    启动时先以普通权限运行，检测系统计划任务中是否存在 'NetflixProxyTool'：
    1. 若未具备管理员权限：
       - 若计划任务已存在：直接通过 schtasks /run /tn "NetflixProxyTool" 以管理员权限静默启动自己，当前进程退出（0 弹窗！）。
       - 若计划任务不存在：调用 ShellExecute 请求一次 UAC 提权启动自己。用户点击一次“是”后，提权后的实例会自动注册计划任务。
    2. 若已具备管理员权限：自动在计划任务中创建/更新该任务（/rl HIGHEST），允许以后免 UAC 静默启动。
    """
    is_admin = False
    try:
        is_admin = (ctypes.windll.shell32.IsUserAnAdmin() != 0)
    except Exception:
        pass

    current_exe = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
    task_name = "NetflixProxyTool"

    if is_admin:
        # 当前已具备管理员权限：确保计划任务已正确注册并指向当前路径
        try:
            check_res = subprocess.run(
                ["schtasks", "/query", "/tn", task_name, "/xml"],
                capture_output=True, text=True, creationflags=0x08000000
            )
            need_register = (check_res.returncode != 0)
            if check_res.returncode == 0 and current_exe.lower() not in check_res.stdout.lower():
                need_register = True

            if need_register:
                tr_target = f'"{current_exe}"' if getattr(sys, 'frozen', False) else f'"{sys.executable}" "{current_exe}"'
                create_cmd = [
                    "schtasks", "/create",
                    "/tn", task_name,
                    "/tr", tr_target,
                    "/sc", "ONCE",
                    "/st", "00:00",
                    "/rl", "HIGHEST",
                    "/f"
                ]
                subprocess.run(create_cmd, capture_output=True, creationflags=0x08000000)
        except Exception:
            pass
        return True

    else:
        # 当前处于非管理员普通权限状态
        try:
            # 确认计划任务存在，且内部注册的文件路径精确指向当前自身的正确位置
            check_res = subprocess.run(
                ["schtasks", "/query", "/tn", task_name, "/xml"],
                capture_output=True, text=True, creationflags=0x08000000
            )
            task_valid = False
            if check_res.returncode == 0 and check_res.stdout:
                # 校验计划任务的 Command 是否包含当前程序的可执行路径
                if current_exe.lower() in check_res.stdout.lower():
                    task_valid = True

            if task_valid:
                # 计划任务存在且指向正确的自己！静默通过计划任务以管理员权限启动
                run_res = subprocess.run(
                    ["schtasks", "/run", "/tn", task_name],
                    capture_output=True, text=True, creationflags=0x08000000
                )
                if run_res.returncode == 0:
                    sys.exit(0)

            # 计划任务不存在，或计划任务中的路径不匹配现在的自己（例如移动了目录或重命名）：
            # 重新获取一次 UAC 授权，启动提权后的自己并将现在的文件地址正确更新进去
            if getattr(sys, 'frozen', False):
                ctypes.windll.shell32.ShellExecuteW(None, "runas", current_exe, "", None, 1)
            else:
                ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{current_exe}"', None, 1)
            sys.exit(0)
        except Exception:
            if getattr(sys, 'frozen', False):
                ctypes.windll.shell32.ShellExecuteW(None, "runas", current_exe, "", None, 1)
            else:
                ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{current_exe}"', None, 1)
            sys.exit(0)

if __name__ == "__main__":
    check_and_elevate_with_task_scheduler()

    try:
        import ctypes
        myappid = 'netflix.proxytool.app.1.0'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
