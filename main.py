# main.py 主逻辑：包括字段拼接、模拟请求
import json
import time
import random
import logging
import hashlib
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
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

def read_with_retry(payload, check_limits, retries):
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
            retries["request"] += 1
            logging.warning("阅读请求暂时失败（%s），%d 秒后重试。", type(exc).__name__, delay)
            time.sleep(delay)


def run_reading(progress, retries):
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
        result = read_with_retry(data, check_limits, retries)

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
            retries["synckey"] += 1
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
            retries["cookie"] += 1
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


def task_label():
    return {
        "7 4 * * *": "12:07 主任务",
        "17 8 * * *": "16:17 补跑",
        "27 10 * * *": "18:27 兜底",
    }.get(os.getenv("WXREAD_SCHEDULE"), "手动任务")


def run_link():
    if os.getenv("GITHUB_RUN_ID"):
        return f"\n{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    return ""


def send_notice(content, success=True):
    if not PUSH_METHOD:
        summary("通知：未配置推送渠道。")
        return 0
    try:
        delivered = push(content + run_link(), PUSH_METHOD, is_success=success)
    except Exception as exc:
        logging.error("通知异常（%s），不修改阅读进度。", type(exc).__name__)
        delivered = False
    summary("通知：推送服务已接收请求。" if delivered else "通知：推送失败，阅读进度不受影响。")
    return 0 if delivered else 1


def notify_start():
    progress = DailyProgress(os.getenv("WXREAD_STATE_FILE"), os.getenv("WXREAD_DATE"))
    if progress.day != beijing_day() or progress.completed >= READ_NUM:
        return 0  # The result notification will report the skipped check.
    return send_notice(
        f"⏳ {task_label()}开始\n今日 {progress.completed * 0.5:g}/{READ_NUM * 0.5:g} 分钟，"
        f"还需 {(READ_NUM - progress.completed) * 0.5:g} 分钟。"
    )


def next_check(day):
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.date().isoformat() != day:
        return None
    for clock in ("12:07", "16:17", "18:27"):
        if clock > now.strftime("%H:%M"):
            return clock
    return None


def notify_result(result=None):
    if result is None:
        path = result_path()
        try:
            result = json.loads(path.read_text(encoding="utf-8")) if path else None
        except (ValueError, OSError):
            result = None
        if not isinstance(result, dict):
            result = {"status": "failed", "date": os.getenv("WXREAD_DATE") or beijing_day(), "completed": None,
                      "error": "任务在完成结果记录前中断，或当天进度恢复失败，请查看 Actions 日志。"}
    completed = result.get("completed")
    minutes = f"{completed * 0.5:g}" if completed is not None else "未知"
    amount = f"今日 {minutes}/{READ_NUM * 0.5:g} 分钟"
    success = result["status"] in ("completed", "skipped")
    if result["status"] == "skipped":
        content = f"✅ 已完成，跳过本次检查｜{amount}"
    elif success:
        content = f"✅ 今日已完成｜{minutes}/{READ_NUM * 0.5:g} 分钟"
    else:
        following = next_check(result["date"])
        final_attempt = os.getenv("WXREAD_SCHEDULE") == "27 10 * * *" or not following
        content = f"{'🔴 兜底未完成，需要处理' if final_attempt else '🟡 本次未完成，等待补跑'}｜{amount}"
        content += "\n" + result["error"]
        if not final_attempt:
            content += f"\n下次计划检查：{following}。"
    content += f"\n{result['date']} · {task_label()}"
    retries = result.get("retries", {})
    recovered = [f"{label} {retries[key]} 次" for key, label in
                 (("request", "请求重试"), ("cookie", "登录刷新"), ("synckey", "同步修复"))
                 if retries.get(key)]
    if recovered:
        content += "\n本次恢复尝试：" + "，".join(recovered)
    if os.getenv("WXREAD_CHECKPOINT_OUTCOME") == "failure":
        content = "🔴 进度保存失败，请检查\n" + content
        success = False
    return send_notice(content, success)


def notify_test():
    if str(PUSH_METHOD).strip().lower() != "telegram":
        raise RuntimeError("请将 PUSH_METHOD 配置为 telegram 后再验证。")
    return send_notice(
        "✅ 微信读书 TG 通知测试\n已启用完成、失败、补跑和晚间核对提醒。\n这是一条通知测试，不代表实际阅读结果。"
    )


def notify_daily():
    day = os.getenv("WXREAD_DATE") or beijing_day()
    path = os.getenv("WXREAD_STATE_FILE")
    if os.getenv("WXREAD_RESTORE_OUTCOME") != "success" or not path or not Path(path).exists():
        send_notice(f"🔴 晚间核对失败｜{day}\n完成状态未知，请检查任务。", False)
        return 1
    try:
        progress = DailyProgress(path, day)
    except (ValueError, OSError, TypeError):
        send_notice(f"🔴 晚间核对失败｜{day}\n进度数据异常，请检查任务。", False)
        return 1
    complete = progress.completed >= READ_NUM
    if complete:
        content = f"✅ 晚间核对正常｜{day}\n已完成 {progress.completed * 0.5:g}/{READ_NUM * 0.5:g} 分钟。"
    else:
        content = (f"🔴 晚间尚未确认完成｜{day}\n"
                   f"已保存 {progress.completed * 0.5:g}/{READ_NUM * 0.5:g} 分钟，请检查并及时补跑。")
    notification_code = send_notice(content, complete)
    return notification_code if complete else 1


def main():
    progress = None
    result = {"date": os.getenv("WXREAD_DATE") or beijing_day(), "status": "failed", "completed": None,
              "retries": {"request": 0, "cookie": 0, "synckey": 0}}
    try:
        progress = DailyProgress(os.getenv("WXREAD_STATE_FILE"), result["date"])
        result["status"] = run_reading(progress, result["retries"])
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
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--notify", action="store_true")
    mode.add_argument("--notify-start", action="store_true")
    mode.add_argument("--notify-test", action="store_true")
    mode.add_argument("--notify-daily", action="store_true")
    args = parser.parse_args()
    handler = main
    for enabled, candidate in ((args.notify, notify_result), (args.notify_start, notify_start),
                               (args.notify_test, notify_test), (args.notify_daily, notify_daily)):
        if enabled:
            handler = candidate
    sys.exit(handler())
