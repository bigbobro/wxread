"""Persist confirmed reading counts, without cookies or notification credentials."""
import io
import argparse
import json
import os
import subprocess
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


def beijing_day():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def audit_day(now=None):
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    # A delayed evening audit after midnight must still check the previous day.
    if now.strftime("%H:%M") < "17:07":
        now -= timedelta(days=1)
    return now.date().isoformat()


def validate_progress(value, day):
    if not isinstance(value, dict) or set(value) != {"schema", "date", "completed"}:
        raise ValueError("阅读进度格式不正确，停止运行以免重复计时。")
    if value["schema"] != 1 or value["date"] != day:
        raise ValueError("阅读进度版本或日期不匹配。")
    count = value["completed"]
    if type(count) is not int or not 0 <= count <= 2880:
        raise ValueError("阅读进度次数不正确。")
    return count


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class DailyProgress:
    def __init__(self, path=None, day=None):
        self.path = Path(path) if path else None
        self.day = day or beijing_day()
        date.fromisoformat(self.day)
        self.completed = 0
        if self.path and self.path.exists():
            value = json.loads(self.path.read_text(encoding="utf-8"))
            # A local file from a previous day does not count toward today's target.
            stored_day = value.get("date") if isinstance(value, dict) else None
            validate_progress(value, stored_day)
            date.fromisoformat(stored_day)
            if stored_day == self.day:
                self.completed = value["completed"]
            elif stored_day > self.day:
                raise ValueError("阅读进度日期在未来，停止运行。")

    def save(self):
        if self.path:
            write_json(self.path, {
                "schema": 1, "date": self.day, "completed": self.completed,
            })

    def advance(self):
        self.completed += 1
        self.save()


def github_api(endpoint):
    for attempt in range(3):
        try:
            return subprocess.check_output(
                ["gh", "api", endpoint], stderr=subprocess.PIPE, timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            if attempt == 2:
                raise RuntimeError("GitHub 进度接口读取失败，保留状态，等待后续补跑。") from exc
            time.sleep((2, 5)[attempt])


def restore_progress(path, day, repository, workflow_path=".github/workflows/deploy.yml"):
    """Restore the greatest confirmed count across this day's trusted runs."""
    date.fromisoformat(day)
    name = f"wxread-progress-{day}"
    restored = DailyProgress(day=day)
    page = 1
    while True:
        query = urlencode({"name": name, "per_page": 100, "page": page})
        payload = json.loads(github_api(f"repos/{repository}/actions/artifacts?{query}"))
        artifacts = payload["artifacts"]
        for artifact in artifacts:
            if artifact["name"] != name or artifact["expired"]:
                continue
            origin = artifact.get("workflow_run") or {}
            if (origin.get("head_branch") != "main"
                    or origin.get("head_repository_id") != origin.get("repository_id")
                    or not origin.get("id")):
                continue
            run = json.loads(github_api(f"repos/{repository}/actions/runs/{origin['id']}"))
            if (run.get("path", "").split("@", 1)[0] != workflow_path
                    or run.get("head_branch") != "main"
                    or run.get("event") not in ("schedule", "workflow_dispatch", "push")
                    or run.get("head_repository", {}).get("full_name", "").lower()
                    != repository.lower()):
                continue
            archive = github_api(f"repos/{repository}/actions/artifacts/{artifact['id']}/zip")
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                info = bundle.getinfo("progress.json")
                if info.file_size > 4096:
                    raise ValueError("阅读进度文件超出预期大小。")
                count = validate_progress(json.loads(bundle.read(info)), day)
            restored.completed = max(restored.completed, count)
        if len(artifacts) < 100:
            break
        page += 1
    restored.path = Path(path)
    restored.save()
    return restored


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    day = audit_day() if args.audit and os.getenv("GITHUB_EVENT_NAME") == "schedule" else beijing_day()
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"day={day}\n")
    restored = restore_progress(
        os.environ["WXREAD_STATE_FILE"], day, os.environ["GITHUB_REPOSITORY"],
    )
    print(f"北京时间 {day}，已恢复 {restored.completed} 次确认成功的阅读。")
