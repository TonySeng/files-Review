"""后端集成冒烟：在临时数据目录启动真实 app，走完 注册→审批→登录→多 Key→scope 网关→管理员密钥管理 全流程。"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = "C:/tmp/bcr_verify_data"
PORT = 9105
BASE = f"http://127.0.0.1:{PORT}"

# 准备临时数据目录
if os.path.isdir(DATA):
    shutil.rmtree(DATA)
os.makedirs(DATA, exist_ok=True)

env = dict(os.environ)
env["BCR_DATA_DIR"] = DATA
env["BCR_ADMIN_USER"] = "admin"
env["BCR_ADMIN_PASSWORD"] = "admin123"


def _free_port(port: int) -> None:
    """释放被上一次中断运行残留的 uvicorn 占用的端口（Windows）。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            encoding="gbk",
            errors="ignore",
            timeout=10,
        ).stdout
    except Exception:
        return
    if not out:
        return
    for line in out.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            pid = line.strip().split()[-1]
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid],
                               capture_output=True, timeout=10)
            except Exception:
                pass


# 释放端口，避免上一轮中断残留的 uvicorn 抢占端口导致命中旧数据
_free_port(PORT)
# 启动 uvicorn（复用当前 venv）
proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

results = []


def call(method, path, *, token=None, api_key=None, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Session-Token", token)
    if api_key:
        req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, None


def check(name, got, expect):
    ok = got == expect
    results.append((name, ok, f"got={got} expect={expect}"))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {got} (期望 {expect})")


# 等待健康
for _ in range(40):
    try:
        st, _ = call("GET", "/api/health")
        if st == 200:
            break
    except Exception:
        pass
    time.sleep(0.5)
else:
    print("SERVER DID NOT START"); proc.terminate(); sys.exit(1)

# 1. admin 登录
st, j = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin123"})
if st != 200:
    print("  DEBUG admin login body:", j)
check("admin_login", st, 200)
ADMIN = j["token"]

# 2. alice 自助注册（pending）
st, _ = call("POST", "/api/auth/register", body={"username": "alice", "password": "secret123", "display_name": "Alice"})
check("alice_register", st, 200)

# 3. alice 审批前登录（期望 403 待审核）
st, _ = call("POST", "/api/auth/login", body={"username": "alice", "password": "secret123"})
check("alice_login_before_approve", st, 403)

# 4. 找到 alice 的 id
st, users = call("GET", "/api/admin/users", token=ADMIN)
ALICE_ID = next(u["id"] for u in users if u["username"] == "alice")
st2, alice = call("GET", f"/api/admin/users/{ALICE_ID}", token=ADMIN)
check("alice_status_pending", alice["status"], "pending")

# 5. 管理员审批
st, alice = call("POST", f"/api/admin/users/{ALICE_ID}/approve", token=ADMIN)
check("approve", st, 200)
check("alice_status_active", alice["status"], "active")

# 6. alice 审批后登录
st, j = call("POST", "/api/auth/login", body={"username": "alice", "password": "secret123"})
check("alice_login_after_approve", st, 200)
ALICE = j["token"]

# 7. alice 创建全权限 Key
st, k1 = call("POST", "/api/users/keys", token=ALICE, body={"name": "prod"})
check("alice_create_key", st, 200)
KEY1 = k1["api_key"]
KEY1_ID = k1["key_id"]

# 8. 全权限 Key 调 /auth/me (200)
st, _ = call("GET", "/api/auth/me", api_key=KEY1)
check("key1_me", st, 200)

# 9. 全权限 Key 调 /review/tasks (scope review, 200)
st, _ = call("GET", "/api/review/tasks", api_key=KEY1)
check("key1_review_tasks", st, 200)

# 10. alice 创建仅 feedback 范围 Key
st, k2 = call("POST", "/api/users/keys", token=ALICE, body={"name": "fb", "scopes": ["feedback"]})
check("alice_create_scoped_key", st, 200)
KEY2 = k2["api_key"]

# 11. feedback 范围 Key 调 /review/tasks (期望 403 scope)
st, _ = call("GET", "/api/review/tasks", api_key=KEY2)
check("key2_review_tasks_scope_denied", st, 403)

# 12. feedback 范围 Key 调 /feedback (scope feedback, 200)
st, _ = call("GET", "/api/feedback", api_key=KEY2)
check("key2_feedback_allowed", st, 200)

# 13. 管理员列出 alice 的密钥
st, ks = call("GET", f"/api/admin/users/{ALICE_ID}/keys", token=ADMIN)
check("admin_list_keys_count", len(ks), 2)

# 14. 管理员禁用全权限 Key -> 该 Key 调 /auth/me (期望 401)
call("PATCH", f"/api/admin/users/{ALICE_ID}/keys/{KEY1_ID}", token=ADMIN, body={"status": "disabled"})
st, _ = call("GET", "/api/auth/me", api_key=KEY1)
check("key1_disabled_rejected", st, 401)

# 15. 管理员吊销 -> 仍 401
call("PATCH", f"/api/admin/users/{ALICE_ID}/keys/{KEY1_ID}", token=ADMIN, body={"status": "revoked"})
st, _ = call("GET", "/api/auth/me", api_key=KEY1)
check("key1_revoked_rejected", st, 401)

# 16. 清理：删除 alice
st, _ = call("DELETE", f"/api/admin/users/{ALICE_ID}", token=ADMIN)
check("delete_alice", st, 200)

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== 后端集成冒烟：{passed}/{len(results)} 通过 =====")
proc.terminate()
sys.exit(0 if passed == len(results) else 1)
