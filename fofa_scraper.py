import os
import sys
import json
import re
import base64
import requests
from playwright.sync_api import sync_playwright

from service_manager import get_system_proxy_url, get_base_dir

DEFAULT_QUERY = 'body="Backend not available" && region="TW"'
DEFAULT_QBASE64 = base64.b64encode(DEFAULT_QUERY.encode('utf-8')).decode('utf-8')
FOFA_SEARCH_URL = f"https://fofa.info/result?qbase64={DEFAULT_QBASE64}"

def get_search_url(page=1, query=DEFAULT_QUERY):
    q_b64 = base64.b64encode(query.encode('utf-8')).decode('utf-8')
    if page <= 1:
        return f"https://fofa.info/result?qbase64={q_b64}"
    return f"https://fofa.info/result?qbase64={q_b64}&page={page}"

DEFAULT_POOLS_TEMPLATE = {
    "hunter": {
        "TW": {"candidates": [], "dead": []},
        "HK": {"candidates": [], "dead": []},
        "JP": {"candidates": [], "dead": []},
        "US": {"candidates": [], "dead": []},
        "KR": {"candidates": [], "dead": []},
        "GB": {"candidates": [], "dead": []},
        "CA": {"candidates": [], "dead": []},
        "SG": {"candidates": [], "dead": []}
    },
    "fofa": {
        "TW": {"candidates": [], "dead": []},
        "HK": {"candidates": [], "dead": []},
        "JP": {"candidates": [], "dead": []},
        "US": {"candidates": [], "dead": []},
        "KR": {"candidates": [], "dead": []},
        "GB": {"candidates": [], "dead": []},
        "CA": {"candidates": [], "dead": []},
        "SG": {"candidates": [], "dead": []}
    }
}

class FofaScraper:
    def __init__(self, config_path=None):
        if config_path is None:
            base_dir = get_base_dir()
            self.config_path = os.path.join(base_dir, "config.json")
            self.pools_path = os.path.join(base_dir, "server_pools.json")
        else:
            self.config_path = config_path
            self.pools_path = os.path.join(os.path.dirname(config_path), "server_pools.json")

        self.config = self.load_config()
        self._pools_cache = None
        self.load_pools()  # 确保 server_pools.json 自动初始化
        self.current_region = self.config.get("target_region", "TW")
        self.current_query = DEFAULT_QUERY

    def set_region(self, region_code):
        if region_code:
            self.current_region = region_code

    def set_query(self, query):
        if query:
            self.current_query = query

    def load_config(self):
        default_cfg = {
            "cookies": {},
            "fofa_token": "",
            "fofa_email": "",
            "fofa_key": "",
            "last_ip": "",
            "custom_ips": [],
            "target_region": "TW",
            "search_source": "hunter"
        }
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        default_cfg.update(data)
                        return default_cfg
            except Exception:
                pass
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(default_cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
        return default_cfg

    def save_config(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"保存配置异常: {e}")

    def import_cookie_string(self, raw_str):
        """
        从文本/复制的请求头中解析 Cookie 并保存
        支持: fofa_token=xxx; other=yyy 格式
        """
        raw_str = raw_str.strip()
        if not raw_str:
            return False, "输入内容为空"

        parsed = {}
        # 支持以 Cookie: 开头的整行请求头
        if raw_str.lower().startswith("cookie:"):
            raw_str = raw_str[7:].strip()

        # 分割分号或换行
        items = re.split(r'[;\n]', raw_str)
        for item in items:
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                parsed[k.strip()] = v.strip()

        if not parsed:
            # 如果只是单个 token 字符串，尝试直接当做 fofa_token
            if len(raw_str) > 20 and "=" not in raw_str:
                parsed["fofa_token"] = raw_str

        if not parsed:
            return False, "未能识别出有效的 Cookie 键值对"

        if "cookies" not in self.config:
            self.config["cookies"] = {}
        self.config["cookies"].update(parsed)
        if "fofa_token" in parsed:
            self.config["fofa_token"] = parsed["fofa_token"]

        self.save_config()
        return True, f"已成功解析并导入 {len(parsed)} 个 Cookie 字段"

    # -------- 本地候选 IP 缓存池管理 (按地区独立持久化保存在 exe 同级目录 server_pools.json 中) --------
    def load_pools(self):
        if os.path.exists(self.pools_path):
            try:
                with open(self.pools_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self._pools_cache = data
                        return data
            except Exception:
                pass
        # 兼容性迁移: 若旧版保存在 config.json 中则自动平滑迁移
        if "server_pools" in self.config and isinstance(self.config["server_pools"], dict):
            pools = self.config["server_pools"]
            self.save_pools(pools)
            return pools
        import copy
        default_pools = copy.deepcopy(DEFAULT_POOLS_TEMPLATE)
        self.save_pools(default_pools)
        return default_pools

    def save_pools(self, pools=None):
        if pools is not None:
            self._pools_cache = pools
        elif getattr(self, "_pools_cache", None) is None:
            return
        try:
            with open(self.pools_path, "w", encoding="utf-8") as f:
                json.dump(self._pools_cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"保存 server_pools.json 异常: {e}")

    def _get_region_pool_dict(self):
        if getattr(self, "_pools_cache", None) is None:
            self._pools_cache = self.load_pools()
        pools = self._pools_cache
        if "fofa" not in pools or not isinstance(pools["fofa"], dict):
            pools["fofa"] = {}
        region = getattr(self, "current_region", "TW")
        if region not in pools["fofa"] or not isinstance(pools["fofa"][region], dict):
            pools["fofa"][region] = {
                "candidates": [],
                "dead": []
            }
            self.save_pools(pools)
        return pools["fofa"][region]

    def get_cached_pool(self):
        pool = self._get_region_pool_dict().get("candidates", [])
        if not isinstance(pool, list):
            pool = []
        return pool

    def get_dead_ips(self):
        dead = self._get_region_pool_dict().get("dead", [])
        if not isinstance(dead, list):
            dead = []
        return dead

    def get_available_pool(self, exclude_ip=None):
        pool = self.get_cached_pool()
        dead = set(self.get_dead_ips())
        if exclude_ip:
            dead.add(exclude_ip)
        return [ip for ip in pool if ip not in dead]

    def add_pool_candidates(self, ips):
        pool_dict = self._get_region_pool_dict()
        pool = pool_dict.get("candidates", [])
        added = 0
        for ip in ips:
            if self.is_valid_real_ipv4(ip) and ip not in pool:
                pool.append(ip)
                added += 1
        pool_dict["candidates"] = pool
        self.save_pools()
        return added

    def mark_ip_dead(self, ip):
        if not ip:
            return
        pool_dict = self._get_region_pool_dict()
        dead = pool_dict.get("dead", [])
        if ip not in dead:
            dead.append(ip)
            pool_dict["dead"] = dead
            self.save_pools()

    def clear_pool(self):
        pool_dict = self._get_region_pool_dict()
        pool_dict["candidates"] = []
        pool_dict["dead"] = []
        self.save_pools()

    @staticmethod
    def is_valid_real_ipv4(ip_str):
        """
        验证是否是合法的非保留公网 IPv4 地址 (过滤 FOFA 未登录返回的 >255 的占位虚假IP)
        """
        parts = ip_str.split('.')
        if len(parts) != 4:
            return False
        try:
            octets = [int(p) for p in parts]
        except ValueError:
            return False
            
        if any(o < 0 or o > 255 for o in octets):
            return False
        # 过滤私网/回环/组播/保留段
        if octets[0] in (0, 10, 127, 255):
            return False
        if octets[0] == 192 and octets[1] == 168:
            return False
        if octets[0] == 172 and 16 <= octets[1] <= 31:
            return False
        return True

    def extract_ips_from_text(self, text):
        """
        从 HTML 或文本中提取所有合法的公网真实 IP
        """
        raw_ips = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text)
        valid_ips = []
        for ip in raw_ips:
            if self.is_valid_real_ipv4(ip) and ip not in valid_ips:
                valid_ips.append(ip)
        return valid_ips

    def fetch_via_api(self, page=1, query=None):
        """
        如果用户配置了 FOFA API Key，直接通过官方 API 快速获取
        """
        email = self.config.get("fofa_email", "").strip()
        key = self.config.get("fofa_key", "").strip()
        if not email or not key:
            return None

        target_query = query or self.current_query or DEFAULT_QUERY
        q_b64 = base64.b64encode(target_query.encode('utf-8')).decode('utf-8')
        api_url = f"https://fofa.info/api/v1/search/all?email={email}&key={key}&qbase64={q_b64}&fields=ip,port&page={page}"
        proxy = get_system_proxy_url()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
            resp = requests.get(api_url, proxies=proxies, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if not data.get("error", True):
                    results = data.get("results", [])
                    ips = []
                    for item in results:
                        ip = item[0] if isinstance(item, list) else item
                        if self.is_valid_real_ipv4(ip) and ip not in ips:
                            ips.append(ip)
                    if ips:
                        self.add_pool_candidates(ips)
                    return ips
        except Exception as e:
            print(f"API 请求失败: {e}")
        return None

    def fetch_via_requests(self, page=1, query=None):
        """
        尝试使用已保存的 Cookie 通过 requests 请求网页获取指定页码
        """
        cookies = self.config.get("cookies", {})
        if not cookies:
            return None

        proxy = get_system_proxy_url()
        proxies = {"http": proxy, "https": proxy} if proxy else None

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
            "Referer": "https://fofa.info/"
        }
        target_query = query or self.current_query or DEFAULT_QUERY
        url = get_search_url(page, query=target_query)
        try:
            resp = requests.get(url, headers=headers, cookies=cookies, proxies=proxies, timeout=12)
            if resp.status_code == 200:
                if "请求速率过快" in resp.text:
                    time.sleep(2)
                    resp = requests.get(url, headers=headers, cookies=cookies, proxies=proxies, timeout=12)

                # 检查是否要求登录
                if "登录" in resp.text and ("个人中心" not in resp.text and "退出" not in resp.text and "hsxa-avatar" not in resp.text):
                    print("Cookie 已失效，需要重新登录")
                    return None
                ips = self.extract_ips_from_text(resp.text)
                if ips:
                    if ips:
                        self.add_pool_candidates(ips)
                    return ips
        except Exception as e:
            print(f"Requests 请求失败 (page {page}): {e}")
        return None

    def interactive_login_and_fetch(self, page=1, log_callback=None, query=None):
        """
        调出 Edge 浏览器窗口，供用户进行 FOFA 登录，登录成功后自动获取 Cookies 和指定页的服务器 IP
        针对 Edge 优化：配置系统代理、防自动化检测、平滑登录跳转
        """
        target_query = query or self.current_query or DEFAULT_QUERY
        if log_callback:
            log_callback("正在启动 Edge 浏览器，请在弹出的窗口中登录 FOFA 账号...")

        captured_ips = []
        with sync_playwright() as p:
            launch_kwargs = {
                "channel": "msedge",
                "headless": False,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-infobars",
                    "--start-maximized"
                ]
            }

            proxy = get_system_proxy_url()
            if proxy:
                launch_kwargs["proxy"] = {"server": proxy}
                if log_callback:
                    log_callback(f"已同步系统网络代理 ({proxy}) 至 Edge 浏览器。")

            try:
                browser = p.chromium.launch(**launch_kwargs)
            except Exception as e:
                if log_callback:
                    log_callback(f"启动 Edge 失败: {e}，尝试使用默认配置启动...")
                launch_kwargs.pop("channel", None)
                browser = p.chromium.launch(**launch_kwargs)

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
                no_viewport=True
            )

            # 注入 stealth 脚本，去除 navigator.webdriver 特征以防被 WAF 拦截
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            # 如果已有 cookies，先注入
            if self.config.get("cookies"):
                cookie_list = []
                for k, v in self.config["cookies"].items():
                    cookie_list.append({
                        "name": str(k),
                        "value": str(v),
                        "domain": ".fofa.info",
                        "path": "/"
                    })
                try:
                    context.add_cookies(cookie_list)
                except Exception:
                    pass

            page_obj = context.new_page()

            # 先打开 FOFA 主页，避免直接打 search 页面触发 ERR_EMPTY_RESPONSE 或频控
            try:
                page_obj.goto("https://fofa.info/", wait_until="commit", timeout=30000)
                page_obj.wait_for_timeout(1000)
            except Exception as e:
                if log_callback:
                    log_callback(f"访问 FOFA 主页提示: {e}")

            # 尝试点击页面上的【登录】按钮，方便用户直接看到登录框
            try:
                login_btn = page_obj.query_selector("button:has-text('登录'), a:has-text('登录')")
                if login_btn and login_btn.is_visible():
                    login_btn.click()
            except Exception:
                pass

            if log_callback:
                log_callback("Edge 浏览器已就绪。请在浏览器中完成 FOFA 登录（登录成功后本工具将自动捕获）。")

            max_wait_seconds = 180
            logged_in = False
            for sec in range(max_wait_seconds):
                try:
                    if page_obj.is_closed():
                        if log_callback:
                            log_callback("浏览器窗口已关闭。")
                        break

                    # 检查 cookies 状态
                    cookies = context.cookies()
                    cookie_dict = {c["name"]: c["value"] for c in cookies}
                    has_token = ("fofa_token" in cookie_dict or "jwt" in cookie_dict or "session" in cookie_dict)

                    content = ""
                    try:
                        content = page_obj.content()
                    except Exception:
                        pass

                    has_user_profile = any(k in content for k in ["个人中心", "退出", "hsxa-avatar", "user-avatar", "会员中心"])

                    if has_token or has_user_profile:
                        logged_in = True
                        if log_callback:
                            log_callback("✅ 登录成功！正在读取 FOFA 服务器数据...")

                        # 登录成功后保存当前最新的 Cookie
                        self.config["cookies"] = cookie_dict
                        if "fofa_token" in cookie_dict:
                            self.config["fofa_token"] = cookie_dict["fofa_token"]
                        self.save_config()

                        # 登录成功后连续抓取第 1 页至第 5 页扩充本地缓存池
                        page_obj.wait_for_timeout(1500)
                        all_captured = []
                        for p in range(1, 6):
                            target_search_url = get_search_url(p, query=target_query)
                            try:
                                page_obj.goto(target_search_url, wait_until="commit", timeout=30000)
                                page_obj.wait_for_timeout(3000)
                            except Exception as e:
                                if log_callback:
                                    log_callback(f"加载 FOFA 第 {p} 页提示: {e}")
                                break

                            search_content = page_obj.content()
                            if "请求速率过快" in search_content:
                                page_obj.wait_for_timeout(3000)
                                search_content = page_obj.content()

                            p_ips = self.extract_ips_from_text(search_content)
                            new_ips = [ip for ip in p_ips if ip not in all_captured]
                            if not new_ips:
                                break
                            all_captured.extend(new_ips)
                            if log_callback:
                                log_callback(f"FOFA 第 {p} 页获取到 {len(new_ips)} 个节点 (累计: {len(all_captured)} 个)")

                        self.add_pool_candidates(all_captured)
                        captured_ips = all_captured
                        if log_callback:
                            log_callback(f"🎉 FOFA 多页采集完毕！共收集到 {len(captured_ips)} 个候选服务器已存入本地池。")
                        break
                except Exception as e:
                    pass

                page_obj.wait_for_timeout(1000)

            try:
                browser.close()
            except Exception:
                pass

        return captured_ips

    def get_candidate_ips(self, page=1, log_callback=None, force_browser_login=False, query=None):
        """
        获取候选 IP 的统一入口（支持指定页码）：
        1. 优先尝试 API
        2. 如果无需强制弹出浏览器，尝试使用已有 Cookie 免弹窗静默获取
        3. 如果均无结果或要求弹窗，则调起 Edge 浏览器交互登录并抓取
        """
        target_query = query or self.current_query or DEFAULT_QUERY
        # 1. 尝试 API
        api_ips = self.fetch_via_api(page=page, query=target_query)
        if api_ips:
            if log_callback:
                log_callback(f"通过 FOFA API 成功获取第 {page} 页 {len(api_ips)} 个候选服务器。")
            return api_ips

        # 2. 尝试基于已存 Cookie 的无界面静默请求
        if not force_browser_login:
            req_ips = self.fetch_via_requests(page=page, query=target_query)
            if req_ips:
                if log_callback:
                    log_callback(f"静默检索第 {page} 页成功，获取到 {len(req_ips)} 个候选服务器。")
                return req_ips
            else:
                if log_callback and page == 1:
                    log_callback("未检测到有效已存凭证，准备调出 Edge 浏览器登录...")

        # 3. 调出浏览器交互登录 (通常仅在 page 1 且未登录时调起)
        if page == 1 or force_browser_login:
            return self.interactive_login_and_fetch(page=page, log_callback=log_callback, query=target_query)
        return []
