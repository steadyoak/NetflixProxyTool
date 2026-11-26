import os
import sys
import json
import re
import time
import base64
import urllib.parse
import requests
from playwright.sync_api import sync_playwright

from service_manager import get_system_proxy_url, get_base_dir

DEFAULT_HUNTER_QUERY = 'web.body="Backend not available"&&ip.province=="中国台湾"'

def get_hunter_b64(query=DEFAULT_HUNTER_QUERY):
    """
    返回 URL-safe Base64 编码的查询语句（去掉末尾 = 填充符）
    """
    return base64.urlsafe_b64encode(query.encode('utf-8')).decode('ascii').rstrip('=')

def get_hunter_search_url(query=DEFAULT_HUNTER_QUERY, page=1):
    b64_q = get_hunter_b64(query)
    # 鹰图 Web 搜索列表 URL
    if page <= 1:
        return f"https://hunter.qianxin.com/list?search={b64_q}"
    return f"https://hunter.qianxin.com/list?search={b64_q}&page={page}"

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

class HunterScraper:
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
        self.current_query = DEFAULT_HUNTER_QUERY

    def set_region(self, region_code):
        if region_code:
            self.current_region = region_code

    def set_query(self, query):
        if query:
            self.current_query = query

    def load_config(self):
        default_cfg = {
            "cookies": {},
            "hunter_cookies": {},
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
        # 文件不存在或损坏，自动初始化
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
        
        # 文件不存在，自动根据模板初始化
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
        if "hunter" not in pools or not isinstance(pools["hunter"], dict):
            pools["hunter"] = {}
        region = getattr(self, "current_region", "TW")
        if region not in pools["hunter"] or not isinstance(pools["hunter"][region], dict):
            pools["hunter"][region] = {
                "candidates": [],
                "dead": []
            }
            self.save_pools(pools)
        return pools["hunter"][region]

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
        """
        获取本地缓存池中尚未失效的备用服务器 IP
        """
        pool = self.get_cached_pool()
        dead = set(self.get_dead_ips())
        if exclude_ip:
            dead.add(exclude_ip)
        return [ip for ip in pool if ip not in dead]

    def add_pool_candidates(self, ips):
        """
        将新抓取到的 IP 存入本地池（自动去重并持久化）
        """
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
        """
        标记不可用 IP，避免下次重复测试已死节点（持久化保存）
        """
        if not ip:
            return
        pool_dict = self._get_region_pool_dict()
        dead = pool_dict.get("dead", [])
        if ip not in dead:
            dead.append(ip)
            pool_dict["dead"] = dead
            self.save_pools()

    def clear_pool(self):
        """
        当当前地区本地池所有 IP 均失效后，清空该地区记录，准备重新拉取新数据
        """
        pool_dict = self._get_region_pool_dict()
        pool_dict["candidates"] = []
        pool_dict["dead"] = []
        self.save_pools()

    @staticmethod
    def is_valid_real_ipv4(ip_str):
        """
        验证是否是合法的非保留公网 IPv4 地址
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
        raw_ips = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text)
        valid_ips = []
        for ip in raw_ips:
            if self.is_valid_real_ipv4(ip) and ip not in valid_ips:
                valid_ips.append(ip)
        return valid_ips

    def import_cookie_string(self, raw_str):
        raw_str = raw_str.strip()
        if not raw_str:
            return False, "输入内容为空"

        parsed = {}
        if raw_str.lower().startswith("cookie:"):
            raw_str = raw_str[7:].strip()

        items = re.split(r'[;\n]', raw_str)
        for item in items:
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                parsed[k.strip()] = v.strip()

        if not parsed:
            if len(raw_str) > 20 and "=" not in raw_str:
                parsed["hunter_token"] = raw_str

        if not parsed:
            return False, "未能识别出有效的 Cookie 键值对"

        if "hunter_cookies" not in self.config:
            self.config["hunter_cookies"] = {}
        self.config["hunter_cookies"].update(parsed)

        self.save_config()
        return True, f"已成功为鹰图导入 {len(parsed)} 个 Cookie 字段"

    def fetch_via_api(self, page=1, page_size=10, log_callback=None, query=None):
        """
        如果用户配置了 Hunter API-Key，直接调用官方 OpenAPI 检索资产
        """
        api_key = self.config.get("hunter_api_key", "").strip()
        if not api_key:
            return None

        target_query = query or self.current_query or DEFAULT_HUNTER_QUERY
        b64_query = get_hunter_b64(target_query)
        api_url = f"https://hunter.qianxin.com/openApi/search?api-key={api_key}&search={b64_query}&page={page}&page_size={page_size}&is_web=3"

        proxy = get_system_proxy_url()
        proxies = {"http": proxy, "https": proxy} if proxy else None

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
        }

        try:
            resp = requests.get(api_url, headers=headers, proxies=proxies, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                code = data.get("code")
                if code == 200:
                    payload = data.get("data", {})
                    arr = payload.get("arr", []) or []
                    rest_quota = payload.get("rest_quota", "")
                    consume_quota = payload.get("consume_quota", "")
                    total = payload.get("total", 0)

                    if log_callback:
                        quota_msg = f" (总计资产: {total} | {consume_quota} | {rest_quota})" if rest_quota else ""
                        log_callback(f"鹰图 API 响应成功{quota_msg}")

                    ips = []
                    for item in arr:
                        ip = item.get("ip") if isinstance(item, dict) else str(item)
                        if ip and self.is_valid_real_ipv4(ip) and ip not in ips:
                            ips.append(ip)
                    if ips:
                        self.add_pool_candidates(ips)
                    return ips
                else:
                    msg = data.get("message") or data.get("msg") or "未知错误"
                    if log_callback:
                        log_callback(f"鹰图 API 返回业务错误 (code {code}): {msg}")
        except Exception as e:
            if log_callback:
                log_callback(f"鹰图 API 请求异常: {e}")
        return None

    def fetch_via_requests(self, page=1, query=None):
        """
        使用保存的 Cookie 进行静默请求
        """
        cookies = self.config.get("hunter_cookies", {})
        if not cookies:
            return None

        proxy = get_system_proxy_url()
        proxies = {"http": proxy, "https": proxy} if proxy else None

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
            "Referer": "https://hunter.qianxin.com/"
        }
        target_query = query or self.current_query or DEFAULT_HUNTER_QUERY
        url = get_hunter_search_url(target_query, page=page)
        try:
            resp = requests.get(url, headers=headers, cookies=cookies, proxies=proxies, timeout=12)
            if resp.status_code == 200:
                if "当前未登录" in resp.text or "登 录" in resp.text:
                    return None
                ips = self.extract_ips_from_text(resp.text)
                if ips:
                    self.add_pool_candidates(ips)
                    return ips
        except Exception:
            pass
        return None

    def interactive_login_and_fetch(self, max_pages=10, log_callback=None, query=None):
        """
        调出 Edge 浏览器窗口，抓取鹰图候选 IP：
        1. 直接访问搜索结果页。
        2. 若检测到未登录，自动点击'登 录'按钮跳转至统一登录页面。
        3. 全面支持微信扫码登录 (hunter.qianxin.com/qrCode)、短信及密码登录，等待用户手机端确认。
        4. 登录完成后自动分析总页数（读取 input[type=number] 的 max 属性），若 <=10 则抓取 1 到 max，若 >10 则抓取 10 页。
        """
        target_query = query or self.current_query or DEFAULT_HUNTER_QUERY
        if log_callback:
            log_callback("正在启动 Edge 浏览器，加载【奇安信鹰图】搜索列表...")

        target_search_url = get_hunter_search_url(target_query, page=1)
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

            # 注入 stealth 脚本，去除 navigator.webdriver 特征
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            # 注入已存 cookies
            if self.config.get("hunter_cookies"):
                cookie_list = []
                for k, v in self.config["hunter_cookies"].items():
                    cookie_list.append({
                        "name": str(k),
                        "value": str(v),
                        "domain": ".qianxin.com",
                        "path": "/"
                    })
                try:
                    context.add_cookies(cookie_list)
                except Exception:
                    pass

            page_obj = context.new_page()

            # 1. 访问目标搜索结果页 (第 1 页)
            try:
                page_obj.goto(target_search_url, wait_until="commit", timeout=30000)
                page_obj.wait_for_timeout(3000)
            except Exception as e:
                if log_callback:
                    log_callback(f"加载鹰图搜索页提示: {e}")

            # 检查当前页面状态
            content = ""
            try:
                content = page_obj.content()
            except Exception:
                pass

            # 精准判断未登录状态
            is_not_logged_in = ("当前未登录" in content)
            login_btn = None
            for sel in ["button:has-text('登 录')", "a:has-text('登 录')", "button:has-text('登录')", "a:has-text('登录')"]:
                try:
                    b = page_obj.query_selector(sel)
                    if b and b.is_visible():
                        is_not_logged_in = True
                        login_btn = b
                        break
                except Exception:
                    pass

            # 2. 如果未登录：自动点击登录按钮
            if is_not_logged_in:
                if log_callback:
                    log_callback("⚠️ 检测到当前尚未登录鹰图（未登录无法使用检索），正在为您自动跳转登录页面...")

                if login_btn:
                    try:
                        login_btn.click()
                        if log_callback:
                            log_callback("已为您点击【登 录】按钮，正在跳转至登录界面...")
                    except Exception as e:
                        if log_callback:
                            log_callback(f"点击登录按钮异常: {e}")
                else:
                    target_redirect = urllib.parse.quote(target_search_url)
                    login_url = f"https://user.skyeye.qianxin.com/user/sign-in?next=https://hunter.qianxin.com/api/uLogin?redirect_path={target_redirect}"
                    try:
                        page_obj.goto(login_url, wait_until="commit", timeout=20000)
                    except Exception:
                        pass

                if log_callback:
                    log_callback("请在 Edge 窗口中完成登录（支持微信扫码 / 短信验证码 / 账号密码）。登录完成前请保持窗口开启...")

                # 3. 轮询等待用户完成登录
                max_wait_seconds = 240
                wechat_prompt_shown = False

                for sec in range(max_wait_seconds):
                    try:
                        if page_obj.is_closed():
                            if log_callback:
                                log_callback("浏览器窗口已关闭。")
                            break

                        curr_url = page_obj.url
                        curr_content = ""
                        try:
                            curr_content = page_obj.content()
                        except Exception:
                            pass

                        # 严格判定登录过程中的 URL 与页面特征：
                        # 包含：user/sign-in, passport, account.qianxin.com, qrCode (微信扫码页), open.weixin.qq.com, uLogin 回调
                        is_in_login_flow = (
                            "user/sign-in" in curr_url or
                            "passport" in curr_url or
                            "account.qianxin.com" in curr_url or
                            "qrcode" in curr_url.lower() or
                            "open.weixin.qq.com" in curr_url or
                            "api/ulogin" in curr_url.lower() or
                            "使用微信扫一扫登录" in curr_content or
                            "个人账号登录" in curr_content or
                            "账号密码登录" in curr_content
                        )

                        if ("qrcode" in curr_url.lower() or "使用微信扫一扫登录" in curr_content) and not wechat_prompt_shown:
                            wechat_prompt_shown = True
                            if log_callback:
                                log_callback("📱 检测到已打开微信扫码界面，请使用手机微信扫码并点击确认登录...")

                        if is_in_login_flow:
                            page_obj.wait_for_timeout(1000)
                            continue

                        # 用户已经离开所有登录/扫码页面，回到了 hunter.qianxin.com
                        if "hunter.qianxin.com" in curr_url:
                            has_login_button = False
                            for sel in ["button:has-text('登 录')", "a:has-text('登 录')"]:
                                try:
                                    b = page_obj.query_selector(sel)
                                    if b and b.is_visible():
                                        has_login_button = True
                                        break
                                except Exception:
                                    pass

                            has_not_logged_text = "当前未登录" in curr_content

                            # 确认已成功通过认证
                            if not has_login_button and not has_not_logged_text:
                                if log_callback:
                                    log_callback("✅ 鹰图账号登录成功！正在保存认证凭据...")
                                cookies = context.cookies()
                                self.config["hunter_cookies"] = {c["name"]: c["value"] for c in cookies}
                                self.save_config()
                                break
                    except Exception:
                        pass

                    page_obj.wait_for_timeout(1000)

            # 4. 已在登录状态或刚完成登录：
            # 鹰图是单页面应用 (SPA)，翻页不变更 URL，而是通过输入框填入页码触发：
            # <div class="q-input q-pagination__editor is-in-pagination"><input type="number" ... min="1" max="26" class="q-input__inner"></div>
            if log_callback:
                log_callback("登录认证成功！正在抓取第 1 页反代服务器并分析总页数...")

            page_obj.wait_for_timeout(2500)
            p1_content = page_obj.content()
            p1_ips = self.extract_ips_from_text(p1_content)
            all_captured_ips = list(p1_ips)
            if log_callback:
                log_callback(f"第 1 页抓取完成，提取到 {len(p1_ips)} 个候选反代服务器。")

            # 读取分页输入框的 max 属性
            max_pages_found = 1
            pagination_input_sel = ".q-pagination__editor input, .q-pagination input[type='number'], input.q-input__inner[type='number']"
            try:
                p_input = page_obj.locator(pagination_input_sel).first
                if p_input.count() > 0:
                    max_attr = p_input.get_attribute("max")
                    if max_attr and max_attr.strip().isdigit():
                        max_pages_found = int(max_attr.strip())
                        if log_callback:
                            log_callback(f"检测到鹰图当前检索结果共有 {max_pages_found} 页。")
            except Exception as e:
                if log_callback:
                    log_callback(f"读取分页信息提示: {e}")

            # 翻页策略：如果小于等于10，把 1 到 max 都收录；如果大于10，截断到10页
            if max_pages_found <= 10:
                target_pages = max(1, max_pages_found)
            else:
                target_pages = 10

            if target_pages > 1:
                if log_callback:
                    log_callback(f"根据翻页策略，将自动连续翻页采集第 2 页至第 {target_pages} 页（总计采集 {target_pages} 页）...")

                for p_num in range(2, target_pages + 1):
                    try:
                        if page_obj.is_closed():
                            break
                        if log_callback:
                            log_callback(f"正在切换至第 {p_num}/{target_pages} 页...")

                        p_input = page_obj.locator(pagination_input_sel).first
                        p_input.click()
                        p_input.fill(str(p_num))
                        p_input.press("Enter")

                        # 等待页面数据更新
                        page_obj.wait_for_timeout(2500)

                        p_content = page_obj.content()
                        p_ips = self.extract_ips_from_text(p_content)
                        new_ips = [ip for ip in p_ips if ip not in all_captured_ips]

                        if not new_ips:
                            # 网络延迟可能，再等1.5秒重试
                            page_obj.wait_for_timeout(1500)
                            p_content = page_obj.content()
                            p_ips = self.extract_ips_from_text(p_content)
                            new_ips = [ip for ip in p_ips if ip not in all_captured_ips]

                        all_captured_ips.extend(new_ips)
                        if log_callback:
                            log_callback(f"第 {p_num} 页采集完成，新增 {len(new_ips)} 个节点 (当前本地池累计: {len(all_captured_ips)} 个)")
                    except Exception as e:
                        if log_callback:
                            log_callback(f"采集第 {p_num} 页异常: {e}")
                        break

            # 保存所有新抓取到的 IP 到本地候选池
            added = self.add_pool_candidates(all_captured_ips)
            if log_callback:
                log_callback(f"🎉 鹰图多页采集完毕！共收集到 {len(all_captured_ips)} 个节点（本地缓存池新增 {added} 个，现有可用备用池总量: {len(self.get_available_pool())} 个）。")

            try:
                browser.close()
            except Exception:
                pass

        return all_captured_ips

    def get_candidate_ips(self, page=1, log_callback=None, force_browser_login=False, query=None):
        """
        获取鹰图候选 IP：优先尝试 API，次选静默请求，最后调出 Edge 多页采集
        """
        target_query = query or self.current_query or DEFAULT_HUNTER_QUERY
        # 1. 尝试 OpenAPI
        api_ips = self.fetch_via_api(page=page, log_callback=log_callback, query=target_query)
        if api_ips:
            if log_callback:
                log_callback(f"通过鹰图 OpenAPI 成功获取第 {page} 页 {len(api_ips)} 个候选服务器。")
            return api_ips

        # 2. 静默请求
        if not force_browser_login:
            req_ips = self.fetch_via_requests(page=page, query=target_query)
            if req_ips:
                if log_callback:
                    log_callback(f"静默检索鹰图第 {page} 页成功，获取到 {len(req_ips)} 个候选服务器。")
                return req_ips
            else:
                if log_callback and page == 1:
                    log_callback("未检测到有效鹰图已存凭据，准备调出 Edge 浏览器进行搜索与登录...")

        # 3. 浏览器交互登录与多页抓取
        if page == 1 or force_browser_login:
            return self.interactive_login_and_fetch(max_pages=10, log_callback=log_callback, query=target_query)
        return []
