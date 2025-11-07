#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
import logging
import requests
from netcup_webservice import NetcupWebservice
from logger import logger
from qb_client import QBittorrentClient
from qb_rss import QBRSSClient

class NetcupTrafficThrottleTester:
    def __init__(self):
        # 固定读取脚本同目录的config.json
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_file = os.path.join(script_dir, 'config.json')

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

        self.qb_rss = None
        if self.vertex_base_url:
            # 供本需求使用：以类形式控制 Vertex 下载器
            self.qb_rss = QBRSSClient(base=self.vertex_base_url, cookie=self.vertex_cookie)

        # 创建Flask应用
        self.app = Flask(__name__)
        self.setup_routes()

        # 启动数据收集线程
        self.data_thread = threading.Thread(target=self.data_collection_loop, daemon=True)
        self.data_thread.start()

        logger.info(f"NetcupTrafficThrottleTester初始化完成")
        logger.info(f"Webhook路径: {self.webhook_path}")
        logger.info(f"端口: {self.port}")
        logger.info(f"配置文件: {self.config_file}")
        logger.info(f"加载了 {len(self.accounts)} 个账户")
        logger.info(f"Vertex: base_url={self.vertex_base_url}")
        logger.info(f"Vertex cookie configured: {bool(self.vertex_cookie)}")

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
                if ipv4_ip in self.cached_data:
                    return jsonify(self.cached_data[ipv4_ip])
                else:
                    return jsonify({"error": f"未找到IP {ipv4_ip} 的信息"}), 404

            except Exception as e:
                logger.error(f"处理webhook请求时发生错误: {e}")
                return jsonify({"error": "内部服务器错误"}), 500

        @self.app.route('/health', methods=['GET'])
        def health():
            return jsonify({
                "status": "ok",
                "timestamp": datetime.now().isoformat(),
                "total_servers": len(self.cached_data)
            })

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
                        # 读取第一个接口的信息（按原需求）
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
        return next(
            (v.get("trafficThrottled") for v in self.cached_data.values() if v.get("ipv4IP") == ip),
            None
        )
    def enable_downloader(self, ip: str):
        if self.qb_rss:
            r = self.qb_rss.enable_downloader(ip)
    
    def disable_downloader(
        self,
        ip: str,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
    
        if self.qb_rss:
            r = self.qb_rss.pause_downloader(ip)

        try:
            qb = QBittorrentClient(url, username, password)
            qb.pause_all()
            time.sleep(5)
            qb.delete_all(delete_files=True)
        except Exception as e:
            logger.error(f"暂停 {ip} 所有任务失败：{e}")
            
        
    def update_cached_data(self):
        """更新缓存的数据，并在状态变化时联动 Vertex 下载器"""
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

            # 对比新旧状态，先不覆盖 cached_data
            for ip, payload in new_data.items():
                new_throttled = payload.get("trafficThrottled")
                old_throttled = self.cached_data.get(ip, {}).get("trafficThrottled")
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
                                #self.enable_downloader(ip)
                            elif new_throttled is True:
                                logger.info(f"[首次-Vertex] 暂停下载器({ip})")
                                #self.disable_downloader(ip, url, username, password)
                                
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
                            #self.enable_downloader(ip)
                        elif old_throttled is False and new_throttled is True:
                            # 暂停 qB 所有任务（该 IP 对应实例）
                            logger.info(f"[Vertex] 暂停下载器({ip})")
                            #self.disable_downloader(ip, url, username, password)
                    except Exception as e:
                        logger.error(f"[联动] 处理 {ip} 的状态变化时出错：{e}")
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
        logger.info(f"Webhook URL: http://localhost:{self.port}{self.webhook_path}")
        logger.info(f"使用方法: GET/POST {self.webhook_path}?ipv4IP=YOUR_IP")
        self.app.run(host='0.0.0.0', port=self.port, debug=False)

def main():
    tester = NetcupTrafficThrottleTester()
    tester.run()

if __name__ == '__main__':
    main()
