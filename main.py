# main.py 主逻辑：包括字段拼接、模拟请求
import json
import time
import random
import logging
import hashlib
import os
import sys
from pathlib import Path
import requests
import urllib.parse
from push import push
from progress import DailyProgress, beijing_day, write_json
from log_utils import setup_logging
from config import data, headers, cookies, READ_NUM, PUSH_METHOD, book, chapter


# 加密盐及其它默认值
KEY = "3c5c8717f3daf09iop3423zafeqoi"
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"
COOKIE_DATA_VARIANTS = [{"rq": "%2Fweb%2Fbook%2Fread", "ql": False},{"rq": "%2Fweb%2Fbook%2Fread", "ql": True},{"rq": "%2Fweb%2Fbook%2Fread"},]
REQUEST_TIMEOUT = (10, 30)
SYNCKEY_REPAIR_LIMIT = 3
SYNCKEY_REPAIR_DELAY = 5
REQUEST_RETRY_DELAYS = (5, 15, 30)
COOKIE_REFRESH_LIMIT = 3


def encode_data(data):
    """数据编码"""
    return '&'.join(f"{k}={urllib.parse.quote(str(data[k]), safe='')}" for k in sorted(data.keys()))


def cal_hash(input_string):
    """计算哈希值"""
    _7032f5 = 0x15051505
    _cc1055 = _7032f5
    length = len(input_string)
    _19094e = length - 1

    while _19094e > 0:
        _7032f5 = 0x7fffffff & (_7032f5 ^ ord(input_string[_19094e]) << (length - _19094e) % 30)
        _cc1055 = 0x7fffffff & (_cc1055 ^ ord(input_string[_19094e - 1]) << _19094e % 30)
        _19094e -= 2

    return hex(_7032f5 + _cc1055)[2:].lower()

def get_wr_skey():
    """刷新cookie密钥"""
    for cookie_data in COOKIE_DATA_VARIANTS:
        try:
            response = requests.post(
                RENEW_URL,
                headers=headers,
                cookies=cookies,
                data=json.dumps(cookie_data, separators=(',', ':')),
                timeout=REQUEST_TIMEOUT,
            )
            
            if 'wr_skey' in response.cookies:
                return response.cookies['wr_skey'][:8]
            else:
                continue
        except requests.RequestException as exc:
            logging.warning(f"refresh_cookie 请求失败，payload={cookie_data}，原因：{exc}")
            continue
        
        
    return None

def fix_no_synckey():
    response = requests.post(
        FIX_SYNCKEY_URL,
        headers=headers,
        cookies=cookies,
        data=json.dumps({"bookIds":["3300060341"]}, separators=(',', ':')),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

refresh_print = setup_logging()

def refresh_cookie(strict=True):
    logging.info("刷新 cookie")
    new_skey = get_wr_skey()
    if new_skey:
        cookies['wr_skey'] = new_skey
        logging.info("密钥刷新成功。")
        logging.info("重新本次阅读。")
        return True

    ERROR_CODE = "无法获取新密钥或者 WXREAD_CURL_BASH 配置有误，终止运行。"
    if strict:
        logging.error(ERROR_CODE)
        raise RuntimeError(ERROR_CODE)

    logging.warning("启动时未获取到新密钥，保留现有 cookie 继续尝试阅读。")
    return False

def read_with_retry(payload, check_limits):
    for attempt in range(len(REQUEST_RETRY_DELAYS) + 1):
        check_limits()
        try:
            response = requests.post(
                READ_URL, headers=headers, cookies=cookies,
                data=json.dumps(payload, separators=(',', ':')),
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code in (401, 403):
                return {"succ": 0}
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Unexpected reading response")
            return result
        except (requests.RequestException, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status and 400 <= status < 500 and status != 429:
                raise RuntimeError(f"阅读接口返回 HTTP {status}，停止本次尝试。") from exc
            if attempt == len(REQUEST_RETRY_DELAYS):
                raise RuntimeError(
                    f"阅读请求连续 {attempt + 1} 次失败（{type(exc).__name__}），等待补跑。"
                ) from exc
            delay = REQUEST_RETRY_DELAYS[attempt]
            logging.warning("阅读请求暂时失败（%s），%d 秒后重试。", type(exc).__name__, delay)
            time.sleep(delay)


def run_reading(progress):
    if not 1 <= READ_NUM <= 2880:
        raise ValueError("READ_NUM 必须在 1 到 2880 之间。")
    deadline = time.monotonic() + int(os.getenv("WXREAD_MAX_RUNTIME_SECONDS", "7200"))

    def check_limits():
        if beijing_day() != progress.day:
            raise RuntimeError("北京时间已跨日，停止本次阅读；新一天由后续任务执行。")
        if time.monotonic() >= deadline:
            raise RuntimeError("本次运行达到时间上限，已确认的进度将交给后续任务补跑。")

    check_limits()
    if progress.completed >= READ_NUM:
        logging.info("今天已完成 %d 次阅读，跳过补跑。", progress.completed)
        return "skipped"
    progress.save()
    # renewal 失败不等于当前会话失效，先保留现有 cookie 尝试阅读。
    refresh_cookie(strict=False)
    if not cookies.get("wr_skey"):
        raise RuntimeError("缺少有效登录 Cookie，请更新 WXREAD_CURL_BASH。")
    last_time = int(time.time()) - 30
    synckey_repairs = 0
    cookie_refreshes = 0
    logging.info("今日目标 %d 次，已完成 %d 次。", READ_NUM, progress.completed)

    while progress.completed < READ_NUM:
        check_limits()
        data.pop('s', None)
        data['b'] = random.choice(book)
        data['c'] = random.choice(chapter)
        this_time = int(time.time())
        data['ct'] = this_time
        data['rt'] = this_time - last_time
        data['ts'] = int(this_time * 1000) + random.randint(0, 1000)
        data['rn'] = random.randint(0, 1000)
        data['sg'] = hashlib.sha256(f"{data['ts']}{data['rn']}{KEY}".encode()).hexdigest()
        data['s'] = cal_hash(encode_data(data))
        result = read_with_retry(data, check_limits)

        if result.get('succ') == 1 and result.get('synckey') is not None:
            # Commit before sleeping or notifying, so a later failure preserves progress.
            progress.advance()
            synckey_repairs = cookie_refreshes = 0
            last_time = this_time
            refresh_print(f"今日阅读进度: {progress.completed}/{READ_NUM} 次，{progress.completed * 0.5:.1f} 分钟")
            time.sleep(30)
        elif result.get('succ') == 1:
            synckey_repairs += 1
            if synckey_repairs > SYNCKEY_REPAIR_LIMIT:
                raise RuntimeError("synckey 连续修复失败，保留进度等待补跑。")
            logging.warning("无 synckey，尝试修复（%d/%d）。", synckey_repairs, SYNCKEY_REPAIR_LIMIT)
            try:
                fix_no_synckey()
            except requests.RequestException as exc:
                logging.warning("synckey 修复请求失败（%s）。", type(exc).__name__)
            time.sleep(SYNCKEY_REPAIR_DELAY)
        else:
            cookie_refreshes += 1
            if cookie_refreshes > COOKIE_REFRESH_LIMIT:
                raise RuntimeError("连续刷新 Cookie 后阅读仍未成功，请检查登录状态或接口变化。")
            logging.warning("阅读未成功，尝试刷新 Cookie（%d/%d）。", cookie_refreshes, COOKIE_REFRESH_LIMIT)
            refresh_cookie()
            time.sleep(5)
    return "completed"


def result_path():
    state_file = os.getenv("WXREAD_STATE_FILE")
    return Path(state_file).with_name("result.json") if state_file else None


def summary(text):
    filename = os.getenv("GITHUB_STEP_SUMMARY")
    if filename:
        with open(filename, "a", encoding="utf-8") as handle:
            handle.write(text + "\n\n")


def notify_result(result=None):
    if result is None:
        path = result_path()
        if path and path.exists():
            result = json.loads(path.read_text(encoding="utf-8"))
        else:
            result = {"status": "failed", "date": beijing_day(), "completed": None,
                      "error": "任务在完成结果记录前中断，或当天进度恢复失败，请查看 Actions 日志。"}
    if result["status"] == "skipped":
        summary("通知：当天已完成，本次跳过，不重复推送。")
        return 0
    if not PUSH_METHOD:
        summary("通知：未配置推送渠道。")
        return 0
    completed = result.get("completed")
    minutes = f"{completed * 0.5:.1f}" if completed is not None else "未知"
    success = result["status"] == "completed"
    content = (f"微信读书：{'已完成' if success else '本次未完成'}\n"
               f"北京时间日期：{result['date']}\n已确认阅读：{minutes}/{READ_NUM * 0.5:.1f} 分钟。")
    if not success:
        content += "\n" + result["error"]
        content += "\n补跑检查时间：16:17、18:27；若今天已无剩余时段，请及时手动检查。"
    if os.getenv("GITHUB_RUN_ID"):
        content += f"\n{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    delivered = push(content, PUSH_METHOD, is_success=success)
    summary("通知：推送服务已接收请求。" if delivered else "通知：推送失败，阅读进度仍独立保留。")
    if not delivered:
        logging.error("推送失败；阅读进度已独立保存，不会因通知失败从头重读。")
    return 0 if delivered else 1


def main():
    progress = None
    result = {"date": os.getenv("WXREAD_DATE") or beijing_day(), "status": "failed", "completed": None}
    try:
        progress = DailyProgress(os.getenv("WXREAD_STATE_FILE"), result["date"])
        result["status"] = run_reading(progress)
    except Exception as exc:
        result["error"] = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else f"运行异常：{type(exc).__name__}"
        logging.error("%s", result["error"])
    if progress is not None:
        result["completed"] = progress.completed
    path = result_path()
    if path:
        write_json(path, result)
    summary(f"阅读日期（北京时间）：{result['date']}\n\n阅读结果：{result['status']}；已确认 {result['completed']}/{READ_NUM} 次。")
    if result.get("error"):
        summary(result["error"])
    code = 1 if result["status"] == "failed" else 0
    if os.getenv("WXREAD_DEFER_NOTIFICATION") != "1":
        code = max(code, notify_result(result))
    return code


if __name__ == "__main__":
    sys.exit(notify_result() if sys.argv[1:] == ["--notify"] else main())
