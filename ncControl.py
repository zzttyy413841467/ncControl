#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
import logging
import requests
from netcup_webservice import NetcupWebservice
from logger import logger
from qb_client import QBittorrentClient
from qb_rss import QBRSSClient
import re

APP_VERSION = "v1.0.6"


class NetcupTrafficThrottleTester:
    def __init__(self):
        # 固定读取脚本同目录的config.json
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_file = os.path.join(script_dir, 'config.json')
        self.frontend_dir = os.path.join(script_dir, 'frontend')  # 前端目录
        self.app_version = APP_VERSION
        
        # 数据缓存 - 存储所有VPS的信息
        # 格式: {"ipv4_ip": {"ipv4IP": "xxx", "trafficThrottled": bool}}
        self.cached_data = {}

        # 加载配置
        config = self.load_config()
        self.webhook_path = config.get('webhook_path', '/webhook/secret-0c68fb14-bb0d-41ca-a53f-a8ba0ea08fae')
        self.port = config.get('port', 56578)
        self.accounts = config.get('accounts', [])

        # Vertex 相关配置（可选，但本需求需要）
        vconf = config.get('vertex', {})
        self.vertex_base_url = vconf.get('base_url', '')
        self.vertex_cookie = vconf.get('cookie', '')
        self.vertex_username = vconf.get('username', '')
        self.vertex_password = vconf.get('password', '')

        # Telegram 相关配置（新增）
        tconf = config.get('telegram', {})
        self.tg_bot_token = tconf.get('bot_token', '')
        self.tg_chat_id = tconf.get('chat_id')

        qconf = config.get('qbittorrent', {})
        self.qb_except_categories = qconf.get('except_categories', '')
        
        self.qb_except_categories_list = self.parse_except_categories(self.qb_except_categories)
        self.bqb_except_categories_list = bool(self.qb_except_categories_list)
        self.throttle_meta = {}
        # 读写 cached_data / throttle_meta 时使用的锁
        self.lock = threading.Lock()

        self.tg_update_offset: int = 0
        self.qb_rss = None
        if self.vertex_base_url:
            # 供本需求使用：以类形式控制 Vertex 下载器
            self.qb_rss = QBRSSClient(base=self.vertex_base_url, cookie=self.vertex_cookie, username=self.vertex_username, password=self.vertex_password)

        # 创建Flask应用
        self.app = Flask(__name__)
        self.setup_routes()

        # 启动数据收集线程
        self.data_thread = threading.Thread(target=self.data_collection_loop, daemon=True)
        self.data_thread.start()

        # 启动 Telegram 轮询线程（不需要 Webhook）
        if self.tg_bot_token:
            self.setup_tg_commands()
            self.tg_thread = threading.Thread(
                target=self.telegram_poll_loop, daemon=True
            )
            self.tg_thread.start()
        logger.info(f"NetcupTrafficThrottleTester初始化完成")
        logger.info(f"端口: {self.port}")
        logger.info(f"配置文件: {self.config_file}")
        logger.info(f"加载了 {len(self.accounts)} 个账户")
        logger.info(f"Vertex: base_url={self.vertex_base_url}")
        logger.info(f"Vertex cookie configured: {bool(self.vertex_cookie)}")
        logger.info(f"Vertex username configured: {bool(self.vertex_username)}")
        logger.info(f"qb except categories list: {self.qb_except_categories_list}")
        logger.info(f"Telegram bot 已配置: {bool(self.tg_bot_token)}")

    def load_config(self):
        """加载配置文件"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                return config
        except FileNotFoundError:
            logger.error(f"配置文件 {self.config_file} 不存在，请创建配置文件")
            return {}
        except json.JSONDecodeError as e:
            logger.error(f"配置文件JSON格式错误: {e}")
            return {}
        except Exception as e:
            logger.error(f"加载配置文件时发生错误: {e}")
            return {}
            
    def parse_except_categories(self, raw: str) -> list[str]:
        """
        兼容：英文逗号/中文逗号/分号/竖线等
        """
        if not raw:
            return []
        parts = re.split(r"[,\uFF0C;；|]+", raw)
        return [p.strip() for p in parts if p.strip()]

			
    def mask_ip(self, ip: str) -> str:
        """ip脱敏操作"""
        parts = ip.split(".")
        if len(parts) != 4:
            return ip
        parts[-1] = "***"
        return ".".join(parts)

    # ---------------- Telegram 相关辅助方法（新增） ----------------

    def send_telegram_message(self, chat_id, text: str, reply_markup: dict | None = None):
        """发送 Telegram 文本消息（简单封装，使用 requests）"""
        if not self.tg_bot_token:
            logger.debug("Telegram bot 未配置，跳过发送消息")
            return
        try:
            url = f"https://api.telegram.org/bot{self.tg_bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            resp = requests.post(url, json=payload, timeout=10)
            if not resp.ok:
                logger.warning(f"发送 Telegram 消息失败: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"发送 Telegram 消息异常: {e}")


    def setup_tg_commands(self):
        """设置 Telegram 左下角菜单按钮中的命令列表"""
        url = f"https://api.telegram.org/bot{self.tg_bot_token}/setMyCommands"
        commands = {
            "commands": [
                {"command": "status", "description": "获取所有nc机器状态"},
                {"command": "version", "description": "获取软件版本编号"},
            ]
        }
        try:
            resp = requests.post(url, json=commands, timeout=10)
            data = resp.json()
            if not data.get("ok", False):
                logger.error(f"设置 Telegram 命令失败: {data}")
            else:
                logger.info("Telegram Bot 命令菜单设置成功")
        except Exception as e:
            logger.error(f"设置 Telegram 命令时出错: {e}")


    def send_telegram_menu(self, chat_id):
        """发送一个简单菜单，包含“获取所有nc机器状态”按钮"""
        keyboard = {
            "keyboard": [
                [{"text": "获取所有nc机器状态"}],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False
        }
        self.send_telegram_message(chat_id, "请选择操作：", reply_markup=keyboard)

    def handle_tg_version_command(self, chat_id):
        """处理“获取软件版本”命令"""
        text = (
            "*当前软件版本*\n"
            f"`{self.app_version}`\n\n"
        )
        self.send_telegram_message(chat_id, text)

    def handle_tg_status_command(self, chat_id):
        """处理“获取所有nc机器状态”命令，快速返回当前缓存状态"""
        with self.lock:
            items = list(self.cached_data.items())

        if not items:
            self.send_telegram_message(chat_id, "当前没有缓存的 Netcup 机器数据，请稍后再试。")
            return

        total = len(items)
        throttled = 0
        lines = []
        for ip, payload in items:
            status = payload.get("trafficThrottled")
            if status:
                throttled += 1
            emoji = "🔴" if status else "🟢"
            masked_ip = self.mask_ip(ip)
            lines.append(f"{emoji} `{masked_ip}` - {'限速中' if status else '正常'}")

        msg = [
            f"*NC 机器状态汇总*",
            f"总数：{total}，当前限速：{throttled} 台",
            "",
            *lines
        ]
        self.send_telegram_message(chat_id, "\n".join(msg))

    def notify_telegram_state_change(self, ip: str, old_throttled, new_throttled):
        """当某个 IP 状态变化时，推送到默认 Telegram 机器人"""
        if not self.tg_bot_token or not self.tg_chat_id:
            # 未配置默认 chat，无通知
            return

        masked_ip = self.mask_ip(ip)
        def state_text(v):
            if v is True:
                return "限速中"
            if v is False:
                return "正常"
            return "未知"

        text = (
            "⚠️ *NC 机器状态变更*\n"
            f"IP：`{masked_ip}`\n"
            f"状态：{state_text(old_throttled)} ➜ {state_text(new_throttled)}\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self.send_telegram_message(self.tg_chat_id, text)

    def telegram_poll_loop(self) :
        """使用 getUpdates 轮询获取 Bot 消息，不依赖 Webhook"""
        logger.info("Telegram 轮询线程已启动")
        base_url = f"https://api.telegram.org/bot{self.tg_bot_token}/getUpdates"

        while True:
            try:
                resp = requests.get(
                    base_url,
                    params={
                        "timeout": 50,
                        "offset": self.tg_update_offset + 1,
                    },
                    timeout=60,
                )
                if resp.status_code != 200:
                    logger.error(f"请求失败，HTTP 状态码: {resp.status_code}")
                    continue
                data = resp.json()
                for update in data.get("result", []):
                    self.tg_update_offset = update.get("update_id", self.tg_update_offset)

                    message = update.get("message") or update.get("edited_message")
                    if not message:
                        continue

                    chat = message.get("chat") or {}
                    chat_id = chat.get("id")
                    if not chat_id or str(chat_id) != str(self.tg_chat_id):
                        continue
                        
                    text = (message.get("text") or "").strip()
                    if not text:
                        continue

                    logger.info(f"收到 Telegram 消息 chat_id={chat_id}, text={text!r}")


                    if text in ("获取所有nc机器状态", "/status"):
                        self.handle_tg_status_command(chat_id)
                    elif text in ("获取软件版本编号", "/version"):
                        self.handle_tg_version_command(chat_id)
                    else:
                        self.send_telegram_message(
                            chat_id,
                            "可用命令：\n"
                            "- /status获取所有nc机器状态：获取所有nc机器状态\n"
                            "- /version获取软件版本：获取软件版本",
                        )
            except requests.exceptions.RequestException as e:
                logger.error(f"请求异常: {e}")
                time.sleep(5)  # 等待 5 秒后重试
            except Exception as e:
                logger.error(f"Telegram 轮询出错: {e}")
                time.sleep(5)

    # ---------------- Flask 路由 ----------------

    def setup_routes(self):
        """设置Flask路由"""
        @self.app.route(self.webhook_path, methods=['GET', 'POST'])
        def webhook():
            try:
                # 获取ipv4IP参数
                ipv4_ip = request.args.get('ipv4IP')
                if not ipv4_ip:
                    return jsonify({"error": "缺少ipv4IP参数"}), 400

                # 从缓存中查找对应的数据
                with self.lock:
                    data = self.cached_data.get(ipv4_ip)
                    
                if data is not None:
                    return jsonify(data)
                return jsonify({"error": f"未找到IP {ipv4_ip} 的信息"}), 404

            except Exception as e:
                logger.error(f"处理webhook请求时发生错误: {e}")
                return jsonify({"error": "内部服务器错误"}), 500

        @self.app.route('/health', methods=['GET'])
        def health():
            with self.lock:
                total_servers = len(self.cached_data)
                
            return jsonify({
                "status": "ok",
                "timestamp": datetime.now().isoformat(),
                "total_servers": total_servers
            })
        # 前端页面
        @self.app.route('/dashboard', methods=['GET'])
        def dashboard():
            """返回前端页面"""
            return send_from_directory(self.frontend_dir, 'index.html')

        # 前端静态文件路由
        @self.app.route('/frontend/<path:path>', methods=['GET'])
        def frontend_assets(path):
            """返回前端静态文件（js, css等）"""
            return send_from_directory(self.frontend_dir, path)

        # 获取状态数据的 API
        @self.app.route('/api/status', methods=['GET'])
        def api_status():
            """
            返回所有 Netcup 机器的限速统计信息：
            - 是否限速（当前）
            - 上一次限速开始时间
            - 上一次限速恢复时间
            - 上一次限速持续多少小时
            - 当前如果正在限速，当前这一轮的开始时间和已持续时长
            """
            with self.lock:
                items = list(self.cached_data.items())
                meta_snapshot = {
                    ip: meta.copy() for ip, meta in self.throttle_meta.items()
                }
                
            now = datetime.now()
            
            # 格式化时间，加入毫秒部分，日期和时间之间用空格分隔
            def format_datetime(dt):
                if dt:
                    # 获取毫秒部分并转化为字符串
                    milliseconds = dt.microsecond // 1000
                    return f"{dt.strftime('%Y-%m-%d-%H:%M:%S')}.{milliseconds:03d}"
                return None
            
            data: list[dict] = []
            for ip, payload in items:
                meta = meta_snapshot.get(
                    ip,
                    {
                        "current_start": None,
                        "last_start": None,
                        "last_end": None,
                        "last_duration_hours": None,
                    },
                )
                traffic_throttled = bool(payload.get("trafficThrottled"))

                current_start = meta.get("current_start")
                last_start = meta.get("last_start")
                last_end = meta.get("last_end")
                last_duration_hours = meta.get("last_duration_hours")
                
                current_start = format_datetime(current_start)
                last_start = format_datetime(last_start)
                last_end = format_datetime(last_end)
                
                # 如果当前正在限速，计算到现在为止的持续时长（仅用于展示）
                current_duration_hours = None
                if traffic_throttled and current_start is not None:
                    delta = now - meta["current_start"]
                    current_duration_hours = round(delta.total_seconds() / 3600.0, 2)
                
                masked_ip  = self.mask_ip(ip)
                data.append({
                    "ipv4IP": masked_ip,
                    "trafficThrottled": traffic_throttled,
                    # 当前一轮限速信息（如果正在限速）
                    "currentThrottleStart": current_start,
                    "currentThrottleDurationHours": current_duration_hours,
                    # 上一次完整限速信息
                    "lastThrottleStart": last_start,
                    "lastThrottleRecover": last_end,
                    "lastThrottleDurationHours": last_duration_hours,
                })

            return jsonify(data)

    def get_vps_info_from_account(self, account):
        """从单个账户获取VPS信息"""
        vps_data = {}
        try:
            # 初始化netcup客户端
            client = NetcupWebservice(
                loginname=account['loginname'],
                password=account['password']
            )

            # 获取所有vserver
            vservers = client.get_vservers()
            logger.info(f"账户 {account['loginname']} 有 {len(vservers)} 个VPS")

            # 获取每个vserver的详细信息
            for vserver_name in vservers:
                try:
                    vserver_info = client.get_vserver_information(vserver_name)

                    # 提取serverInterfaces中的ipv4IP和trafficThrottled
                    if 'serverInterfaces' in vserver_info and vserver_info['serverInterfaces']:
                        # 读取第一个接口的信息
                        interface = vserver_info['serverInterfaces'][0]

                        try:
                            ipv4_ips = getattr(interface, 'ipv4IP', [])
                            traffic_throttled = getattr(interface, 'trafficThrottled', False)

                            logger.debug(f"从接口获取到: ipv4IP={ipv4_ips}, trafficThrottled={traffic_throttled}")

                            if not isinstance(ipv4_ips, list):
                                ipv4_ips = [ipv4_ips] if ipv4_ips else []

                            for ipv4_ip in ipv4_ips:
                                if ipv4_ip:
                                    vps_data[ipv4_ip] = {
                                        "ipv4IP": ipv4_ip,
                                        "trafficThrottled": bool(traffic_throttled)
                                    }
                                    logger.info(f"成功添加VPS信息: {ipv4_ip} -> trafficThrottled: {traffic_throttled}")

                        except Exception as attr_error:
                            logger.error(f"访问接口属性时出错: {attr_error}")
                            logger.debug(f"接口对象类型: {type(interface)}")
                            try:
                                if hasattr(interface, '__dict__'):
                                    logger.debug(f"接口对象属性: {interface.__dict__}")
                                else:
                                    logger.debug(f"接口对象内容: {interface}")
                            except:
                                logger.debug("无法打印接口对象详情")
                            continue

                except Exception as e:
                    logger.error(f"获取VPS {vserver_name} 信息失败: {e}")
                    continue

        except Exception as e:
            logger.error(f"从账户 {account['loginname']} 获取VPS信息失败: {e}")

        return vps_data

    def get_traffic_throttled_by_value(self, ip: str):
        """获取指定 IP 当前的 trafficThrottled 状态"""
        with self.lock:
            info = self.cached_data.get(ip)
        if info is None:
            return None
        return info.get("trafficThrottled")
        
    def enable_downloader(self, ip: str):
        if not self.qb_rss:
            logger.warning(f"未配置 Vertex, 无法启用下载器{ip}")
            return
            
        try:
            self.qb_rss.enable_downloader(ip)
        except Exception as e:
            logger.error(f"启用 {ip} 下载器失败：{e}")
    
    def disable_downloader(
        self,
        ip: str,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        except_categories: bool = False
    ):
    
        if self.qb_rss:
            try:
                self.qb_rss.pause_downloader(ip)
            except Exception as e:  
                logger.error(f"暂停 {ip} 下载器{ip} 失败：{e}")

        try:
            qb = QBittorrentClient(url, username, password)
            if except_categories:
                qb.stop_report_delete_all_except_categories(self.qb_except_categories_list)
            else:
                qb.pause_all()
                time.sleep(5)
                qb.delete_all(delete_files=True)
        except Exception as e:
            logger.error(f"暂停 {ip} 所有任务失败：{e}")
            
        
    def update_cached_data(self):
        """更新缓存的数据，并在状态变化时联动 Vertex 下载器 + 推送 Telegram"""
        try:
            new_data = {}

            # 遍历所有配置的账户
            for account in self.accounts:
                if 'loginname' not in account or 'password' not in account:
                    logger.warning(f"账户配置不完整，跳过: {account}")
                    continue

                #logger.info(f"正在从账户 {account['loginname']} 获取VPS信息...")
                account_data = self.get_vps_info_from_account(account)
                new_data.update(account_data)
            now = datetime.now()  # 新增：统一使用当前时间

            with self.lock:
                # 对比新旧状态，先不覆盖 cached_data
                for ip, payload in new_data.items():
                    new_throttled = payload.get("trafficThrottled")
                    old_throttled = self.cached_data.get(ip, {}).get("trafficThrottled")
    
                    # 确保 throttle_meta 里有这个 IP 的结构
                    meta = self.throttle_meta.setdefault(ip, {
                        "current_start": None,
                        "last_start": None,
                        "last_end": None,
                        "last_duration_hours": None,
                    })
    
                    url, username, password = self.qb_rss.get_user_info(ip)
                    if url is None or username is None or password is None:
                        continue
                        
                    logger.info(f"url : {url}, username :{username}, password ：{password}")
                    
                    if old_throttled is None:
                        # 首次发现
                        logger.info(f"[状态监听] 首次发现 {ip}，trafficThrottled={new_throttled}")
                        # 按你之前的业务规则： 
                        # False -> 启用下载器；True -> 暂停所有任务并暂停下载器
                        try:
                            if new_throttled is False:
                                logger.info(f"[首次-Vertex] 启用下载器({ip})")
                                self.enable_downloader(ip)
                            elif new_throttled is True:
                                logger.info(f"[首次-Vertex] 暂停下载器({ip})")
                                meta["current_start"] = now
                                self.disable_downloader(ip, url, username, password, self.bqb_except_categories_list)
                                
                        except Exception as e:
                            logger.error(f"[首次-联动] 处理 {ip} 时出错：{e}")
                        
                    elif old_throttled != new_throttled:
                        logger.warning(f"[状态变化] {ip}: {old_throttled} -> {new_throttled}")
                        # ---- 业务逻辑：
                        # 1) 若 True -> False（解除限速）：启用该下载器（允许进入“限速态”下的收割流程，具体按你的面板策略）
                        # 2) 若 False -> True（被限速）：暂停该 IP 的所有 qB 任务，并暂停该下载器（避免瞬时冲高）
                        try:
                            if old_throttled is True and new_throttled is False:
                                logger.info(f"[Vertex] 启用下载器({ip})")
                                self.enable_downloader(ip)
                                if meta.get("current_start") is not None:
                                    meta["last_start"] = meta["current_start"]
                                    meta["last_end"] = now
                                    delta = now - meta["current_start"]
                                    meta["last_duration_hours"] = round(
                                        delta.total_seconds() / 3600.0, 2
                                    )
                                meta["current_start"] = None
                            elif old_throttled is False and new_throttled is True:
                                # 暂停 qB 所有任务（该 IP 对应实例）
                                meta["current_start"] = now
                                logger.info(f"[Vertex] 暂停下载器({ip})")
                                self.disable_downloader(ip, url, username, password, self.bqb_except_categories_list)
                        except Exception as e:
                            logger.error(f"[联动] 处理 {ip} 的状态变化时出错：{e}")

                        # 状态变更时，推送到 Telegram（新增）
                        self.notify_telegram_state_change(ip, old_throttled, new_throttled)
                    else:
                        logger.debug(f"[状态监听] {ip} 未变化：{new_throttled}")

                # 更新缓存
                self.cached_data = new_data
                logger.info(f"数据更新成功，共缓存 {len(self.cached_data)} 个VPS IP信息")
                for key, value in self.cached_data.items():
                    logger.info(f"缓存的详细信息 ipv4IP={value.get('ipv4IP')}, trafficThrottled={value.get('trafficThrottled')}")

        except Exception as e:
            logger.error(f"更新缓存数据时发生错误: {e}")

    def data_collection_loop(self):
        """数据收集循环，每5分钟执行一次"""
        logger.info("数据收集线程已启动")

        # 立即执行一次数据更新
        self.update_cached_data()

        while True:
            try:
                time.sleep(300)  # 5分钟 = 300秒
                self.update_cached_data()
            except Exception as e:
                logger.error(f"数据收集循环中发生错误: {e}")
                time.sleep(60)  # 发生错误时等待1分钟后重试

    def run(self):
        """启动Flask应用"""
        logger.info(f"启动Web服务，端口: {self.port}")
        self.app.run(host='0.0.0.0', port=self.port, debug=False)

def main():
    tester = NetcupTrafficThrottleTester()
    tester.run()

if __name__ == '__main__':
    main()
