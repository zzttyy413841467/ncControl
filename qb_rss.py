import json
import hashlib
from http.cookiejar import CookieJar
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPCookieProcessor
from logger import logger

class QBRSSClient:
    """
    负责启用/暂停 Vertex 的下载器（通常对应某台 qB 客户端）。

    ✅ 支持三种方式（自动降级/恢复）：
    1) 仅 cookie：一直用你传入的 Cookie 请求
    2) 仅账密：启动即登录，使用会话 Cookie
    3) cookie + 账密：优先用 cookie；如果 cookie 失效（401/403 或“未登录/unauthorized”），自动用账密重新登录并重试一次
    """
    def __init__(
        self,
        base: str,
        cookie: str = "",
        username: str = "",
        password: str = "",
        otpPw: str = "",
        timeout: int = 30,
    ):
        self.base = (base or "").rstrip("/")
        self.cookie = cookie or ""
        self.timeout = int(timeout)

        # 保存账密，用于 cookie 失效时自动重登
        self._username = (username or "").strip()
        self._password_plain = password or ""
        self._otpPw = otpPw or ""

        self.headers = {"Accept": "application/json"}
        if self.cookie:
            self.headers["Cookie"] = self.cookie
            self._opener = build_opener() 

        # 若未显式传 cookie，但提供了账密，则启动即登录
        if (not self.cookie) and self._username and self._password_plain:
            self._do_login()

    # ----------------- 账密登录 -----------------
    @staticmethod
    def _md5_hex(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def _do_login(self) -> dict:
        """
        真正执行登录：不做“失败再重登”的重试逻辑，避免递归。
        登录成功后服务器通常会 Set-Cookie，cookiejar 会自动接收并用于后续请求。
        """
        payload = {
            "username": self._username,
            "password": self._md5_hex(self._password_plain),
            "otpPw": self._otpPw or None,
        }
        return self._http_post_json_raw(f"{self.base}/api/user/login", payload)


    # ----------------- 认证失败判定 & 自动重登 -----------------
    @staticmethod
    def _looks_like_auth_failure(data: object) -> bool:
        """
        根据接口返回 JSON 判断是否像“未登录/权限不足”。
        你们前端 axios.js 是看 success/message，这里做兼容判断。
        """
        if not isinstance(data, dict):
            return False
        if data.get("success") is True:
            return False

        msg = str(data.get("message") or data.get("msg") or data.get("error") or "").lower()
        keywords = [
            "未登录", "请登录", "登录", "无权限", "权限不足",
            "unauthorized", "forbidden", "not logged", "login required",
            "token", "session",
        ]
        return any(k.lower() in msg for k in keywords)

    def _can_reauth(self) -> bool:
        return bool(self._username and self._password_plain)

    def _reauth_and_retry_prepare(self) -> None:
        """
        当你传入的 Cookie 失效时：
        - 必须移除 headers 里的旧 Cookie（否则它会一直覆盖 cookiejar 的新 cookie）
        - 清空 jar（可选但更干净），再登录获取新会话
        """
        if "Cookie" in self.headers:
            self.headers.pop("Cookie", None)
        self.cookie = ""

        # 重置 cookiejar（避免保留旧的失效 cookie）
        self._jar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._jar))
        logger.info("do_login")
        # 执行登录
        self._do_login()

    # ----------------- 内部 HTTP 工具 -----------------
    def _http_get_json_raw(self, url: str) -> dict:
        req = Request(url, headers=self.headers, method="GET")
        with self._opener.open(req, timeout=self.timeout) as resp:
            data = resp.read()
        return json.loads(data.decode("utf-8"))

    def _http_post_json_raw(self, url: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        h = dict(self.headers)
        h["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=h, method="POST")
        with self._opener.open(req, timeout=max(self.timeout, 45)) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))

    def _http_get_json(self, url: str) -> dict:
        """
        GET（带自动重登重试一次）：
        - 若 cookie 失效且提供了账密，则自动登录后重试一次
        """
        try:
            data = self._http_get_json_raw(url)
        except Exception:
            logger.info("cookies已失效，使用账密登陆")
            if self._can_reauth():
                self._reauth_and_retry_prepare()
                return self._http_get_json_raw(url)
            raise

        if self._looks_like_auth_failure(data) and self._can_reauth():
            self._reauth_and_retry_prepare()
            return self._http_get_json_raw(url)

        return data

    def _http_post_json(self, url: str, payload: dict) -> dict:
        """
        POST（带自动重登重试一次）
        """
        try:
            data = self._http_post_json_raw(url, payload)
        except Exception:
            if self._can_reauth():
                self._reauth_and_retry_prepare()
                return self._http_post_json_raw(url, payload)
            raise

        if self._looks_like_auth_failure(data) and self._can_reauth():
            self._reauth_and_retry_prepare()
            return self._http_post_json_raw(url, payload)

        return data

    # ----------------- 原逻辑（基本不变） -----------------
    @staticmethod
    def _extract_host(client_url: str) -> str | None:
        if not isinstance(client_url, str):
            return None
        # 优先解析标准 URL
        try:
            host = urlsplit(client_url).hostname
            if host:
                return host
        except Exception:
            pass
        if "://" not in client_url and ":" in client_url:
            return client_url.split(":")[0]
        return None

    @staticmethod
    def _find_downloader(downloaders: list, key: str) -> dict | None:
        """
        key 可以是下载器ID，或 IP（将与 clientUrl 的 hostname 匹配）。
        返回完整的下载器对象。
        """
        # 先当作ID精确匹配
        for d in downloaders:
            if d.get("id") == key or d.get("_id") == key:
                return d
        # 再按 IP/hostname 匹配 clientUrl
        for d in downloaders:
            cu = d.get("clientUrl") or d.get("url") or ""
            host = QBRSSClient._extract_host(cu) if cu else None
            if host is not None and host == key:
                return d
        return None
        
    def get_qb_info(self, ip_or_id: str):            
        # 1) 拉取下载器列表
        dlist = self._http_get_json(f"{self.base}/api/downloader/list")
        downloaders = dlist.get("data") if isinstance(dlist, dict) else dlist
        if not isinstance(downloaders, list):
            return {"ok": False, "detail": "Unexpected /api/downloader/list response"}

        # 2) 定位目标下载器
        target = self._find_downloader(downloaders, ip_or_id)

        return target
        
        
    def get_user_info(self, ip:str):
        target = self.get_qb_info(ip)
        if not target:
            return None, None, None
            
        url = target.get("clientUrl") or target.get("url") or None
        username = target.get("username") or target.get("user") or target.get("login") or None
        password = target.get("password") or target.get("pass") or None
        
        return (url, username, password)

    # ----------------- 对外能力 -----------------
    def set_downloader_enabled(self, ip_or_id: str, enabled: bool) -> dict:
        """
        启用/禁用（暂停） Vertex 下载器。
        - ip_or_id: 可以传 IP（匹配 downloader.clientUrl 的 hostname），也可以直接传下载器ID
        - enabled: True=启用, False=禁用(暂停)
        返回：{"ok": True/False, "id": "...", "alias": "...", "verify": {"enable": ..., "enabled": ...}, "detail": ...}
        """
        
        if not self.base:
            return {"ok": False, "detail": "base url not configured"}

        target = self.get_qb_info(ip_or_id)
        if not target:
            return {"ok": False, "detail": f"Downloader not found for key={ip_or_id}"}

        did = target.get("id") or target.get("_id")
        alias = target.get("alias")
        
        # 3) 提交修改（完整对象，避免丢字段；兼容 enable/enabled 命名差异）
        new_obj = dict(target)
        new_obj["enable"] = bool(enabled)   # 常见字段

        resp = self._http_post_json(f"{self.base}/api/downloader/modify", new_obj)

        # 4) 验证
        vlist = self._http_get_json(f"{self.base}/api/downloader/list")
        vdownloaders = vlist.get("data") if isinstance(vlist, dict) else vlist
        v = next((d for d in vdownloaders if (d.get("id") or d.get("_id")) == did), None)
        verify = {"enable": v.get("enable") if v else None, "enabled": v.get("enabled") if v else None}

        return {"ok": True, "id": did, "alias": alias, "verify": verify, "detail": resp}

    def pause_downloader(self, ip_or_id: str) -> dict:
        """暂停（禁用）下载器。"""
        return self.set_downloader_enabled(ip_or_id, False)

    def enable_downloader(self, ip_or_id: str) -> dict:
        """启用下载器。"""
        return self.set_downloader_enabled(ip_or_id, True)