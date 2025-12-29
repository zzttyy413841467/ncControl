import os
import time
from qbittorrentapi import Client
from logger import logger

class QBittorrentClient:
    """
    使用 qbittorrent-api 实现的精简版客户端，添加了种子状态检查功能。
    """

    def __init__(
        self,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        """
        初始化客户端，自动从环境变量中加载配置。
        """
        self.host = url
        self.username = username
        self.password = password

        try:
            self.client = Client(host=self.host, username=self.username, password=self.password, VERIFY_WEBUI_CERTIFICATE=False)
            self.client.auth_log_in()
            logger.info(f"成功连接到 qBittorrent API: {self.host}")
        except Exception as e:
            raise ConnectionError(f"连接 qBittorrent API 失败: {e}")

    def is_alive(self) -> bool:
        """简易健康检查：尝试请求应用版本。"""
        try:
            ver = self.client.app.version
            logger.info(f"{self.host} app version is {ver}")
            return True
        except Exception:
            return False

    def stop_report_delete_all_except_categories(
        self,
        exclude_categories,
        *,
        delete_files: bool = True,
        wait_seconds: int = 5,
    ) -> int:
        """
        除 exclude_categories 外：强制汇报 + 暂停 + 删除（可选删除文件）
        :param exclude_categories: 要保留(不删除)的分类名，支持 str 或 list/set/tuple[str]
        :param delete_files: True 时会连同本地数据一并删除（危险操作）默认 True
        :param wait_seconds: 强制汇报后等待秒数（给 tracker 一点时间）
        :return: 实际处理（删除）的任务数量
        """
        if isinstance(exclude_categories, str):
            exclude_set = {exclude_categories}
        else:
            exclude_set = set(exclude_categories or [])

        # 兼容 qbittorrent-api 不同版本的调用方式
        if hasattr(self.client, "torrents_info"):
            torrents = self.client.torrents_info()
        else:
            torrents = self.client.torrents.info()

        # 过滤：category 在 exclude_set 的不动，其它全处理
        hashes = []
        for t in torrents:
            h = getattr(t, "hash", None)
            cat = getattr(t, "category", None)
            if not h:
                continue
            if cat in exclude_set:
                continue
            hashes.append(h)

        if not hashes:
            logger.info(f"未找到可处理任务（排除分类: {sorted(exclude_set)}）。")
            return 0

        hash_str = "|".join(hashes)

        # 1) 强制汇报
        try:
            self.client.torrents_reannounce(torrent_hashes=hash_str)
            logger.info(f"已对 {len(hashes)} 个任务发出强制汇报指令（排除分类: {sorted(exclude_set)}）。")
        except Exception as e:
            logger.warning(f"强制汇报失败（仍继续后续操作）：{e}")

        # 给 tracker 一点时间（可调）
        if wait_seconds and wait_seconds > 0:
            time.sleep(wait_seconds)

        # 2) 暂停（stop）
        try:
            self.client.torrents_stop(torrent_hashes=hash_str)
            logger.info(f"已对 {len(hashes)} 个任务发出暂停指令（排除分类: {sorted(exclude_set)}）。")
        except Exception as e:
            logger.warning(f"暂停失败（仍继续后续操作）：{e}")

        # 3) 删除（种子 + 文件）
        self.client.torrents.delete(hashes=hash_str, delete_files=delete_files)
        logger.info(
            f"已发出删除指令：删除 {len(hashes)} 个任务（排除分类: {sorted(exclude_set)}），delete_files={delete_files}"
        )
        return len(hashes)


    def pause_all(self):
        """
        暂停所有种子任务，并检查是否全部暂停成功。
        """
        self.client.torrents_reannounce(torrent_hashes="all")
        self.client.torrents_stop(torrent_hashes="all")
        
        logger.info("已发出强制汇报&暂停所有种子任务的指令。")
            
    def delete_all(self, *, delete_files: bool = False) -> None:
        """
        删除全部任务。
        :param delete_files: True 时会连同本地数据一并删除（危险操作）
        """
        self.client.torrents.delete(hashes="all", delete_files=delete_files)
        logger.info("已发出删除全部任务的指令。")

## 测试代码
#if __name__ == "__main__":
#    # 初始化客户端
#    qb_client = QBittorrentClient("185.244.194.39")
#    
#    qb_client.is_alive()
#    
#    qb_client.exit_block_new()
