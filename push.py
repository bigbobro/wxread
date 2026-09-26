import json
import logging
import os
import time
from urllib.parse import quote

import requests

from config import (
    PUSHPLUS_TOKEN,
    SERVERCHAN_SPT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WXPUSHER_SPT,
)

logger = logging.getLogger(__name__)
RETRY_DELAYS = (5, 15, 30, 60)


def require_accepted(response, field, expected):
    response.raise_for_status()
    payload = response.json()
    value = payload.get(field) if isinstance(payload, dict) else None
    if type(value) is not type(expected) or value != expected:
        raise ValueError("推送服务未接受请求。")


class PushNotification:
    def __init__(self):
        self.pushplus_url = "https://www.pushplus.plus/send"
        self.telegram_url = "https://api.telegram.org/bot{}/sendMessage"
        self.server_chan_url = "https://sctapi.ftqq.com/{}.send"
        self.wxpusher_simple_url = "https://wxpusher.zjiecode.com/api/send/message/{}/{}"
        self.headers = {"Content-Type": "application/json"}
        self.proxies = {
            "http": os.getenv("http_proxy"),
            "https": os.getenv("https_proxy"),
        }

    def push_pushplus(self, content, token, is_success):
        attempts = 5
        title = f"微信阅读-{'成功' if is_success else '失败'}"
        for attempt in range(attempts):
            try:
                response = requests.post(
                    self.pushplus_url,
                    data=json.dumps({"token": token, "title": title,"content": content,}).encode("utf-8"),headers=self.headers,timeout=10,)
                require_accepted(response, "code", 200)
                logger.info("PushPlus 已接收推送请求。")
                return True
            except (requests.exceptions.RequestException, ValueError) as exc:
                logger.error("PushPlus 推送失败（%s）。", type(exc).__name__)
                if attempt < attempts - 1:
                    sleep_time = RETRY_DELAYS[attempt]
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_telegram(self, content, bot_token, chat_id):
        url = self.telegram_url.format(bot_token)
        payload = {"chat_id": chat_id, "text": content}

        try:
            response = requests.post(url, json=payload, proxies=self.proxies, timeout=30)
            require_accepted(response, "ok", True)
            logger.info("Telegram 已接收推送请求。")
            return True
        except Exception as exc:
            logger.error("Telegram 代理发送失败（%s）。", type(exc).__name__)
            try:
                response = requests.post(url, json=payload, timeout=30)
                require_accepted(response, "ok", True)
                return True
            except Exception as inner_exc:
                logger.error("Telegram 发送失败（%s）。", type(inner_exc).__name__)
                return False

    def push_wxpusher(self, content, spt):
        attempts = 5
        url = self.wxpusher_simple_url.format(spt, quote(content, safe=""))

        for attempt in range(attempts):
            try:
                response = requests.get(url, timeout=10)
                require_accepted(response, "code", 1000)
                logger.info("WxPusher 已接收推送请求。")
                return True
            except (requests.exceptions.RequestException, ValueError) as exc:
                logger.error("WxPusher 推送失败（%s）。", type(exc).__name__)
                if attempt < attempts - 1:
                    sleep_time = RETRY_DELAYS[attempt]
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_serverChan(self, content, spt, is_success):
        attempts = 5
        url = self.server_chan_url.format(spt)

        title = f"微信阅读-{'成功' if is_success else '失败'}"

        for attempt in range(attempts):
            try:
                response = requests.post(
                    url,
                    data=json.dumps({"title": title, "desp": content}).encode("utf-8"),
                    headers=self.headers,
                    timeout=10,
                )
                require_accepted(response, "code", 0)
                logger.info("ServerChan 已接收推送请求。")
                return True
            except (requests.exceptions.RequestException, ValueError) as exc:
                logger.error("ServerChan 推送失败（%s）。", type(exc).__name__)
                if attempt < attempts - 1:
                    sleep_time = RETRY_DELAYS[attempt]
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False


def push(content, method, is_success = True):
    notifier = PushNotification()

    if method in (None, ""):
        logger.warning("未配置推送渠道，跳过推送。")
        return False

    method = str(method).lower()

    if method == "pushplus":
        return notifier.push_pushplus(content, PUSHPLUS_TOKEN, is_success)
    if method == "telegram":
        return notifier.push_telegram(content, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    if method == "wxpusher":
        return notifier.push_wxpusher(content, WXPUSHER_SPT)
    if method == "serverchan":
        return notifier.push_serverChan(content, SERVERCHAN_SPT, is_success)

    logger.warning("无效的通知渠道 '%s'，已跳过推送。支持：pushplus、telegram、wxpusher、serverchan", method)
    return False
