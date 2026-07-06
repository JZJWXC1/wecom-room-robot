"""check_unattended_runtime 回调路由探测的单元测试。

覆盖企微客服回调 IP:80 /wecom 路由健康探测的状态归类与 curl 封装：
- 404 归为 route_404（Certbot 重写抹掉 /wecom 转发导致的历史故障信号）；
- 200/422 归为 ok（422 为缺签名参数的裸探测，属正常已路由）；
- curl 失败/异常有明确降级返回，不抛异常污染 readiness 判定。
"""

from scripts import check_unattended_runtime as cur


def test_classify_callback_code_404_is_route_404():
    assert cur._classify_callback_code("404") == "route_404"


def test_classify_callback_code_healthy_codes():
    assert cur._classify_callback_code("200") == "ok"
    assert cur._classify_callback_code("422") == "ok"


def test_classify_callback_code_unexpected_and_empty():
    assert cur._classify_callback_code("500") == "unexpected:500"
    assert cur._classify_callback_code("502") == "unexpected:502"
    assert cur._classify_callback_code("") == "no_code"
    assert cur._classify_callback_code("  ") == "no_code"


class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_callback_route_status_ok(monkeypatch):
    monkeypatch.setattr(
        cur.subprocess, "run", lambda *a, **k: _FakeCompleted(0, stdout="422")
    )
    assert cur._callback_route_status("http://x/wecom/kf/callback", "1.2.3.4") == "ok"


def test_callback_route_status_route_404(monkeypatch):
    monkeypatch.setattr(
        cur.subprocess, "run", lambda *a, **k: _FakeCompleted(0, stdout="404")
    )
    assert cur._callback_route_status("http://x/wecom/kf/callback", "1.2.3.4") == "route_404"


def test_callback_route_status_curl_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        cur.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(7, stdout="", stderr="Failed to connect"),
    )
    status = cur._callback_route_status("http://x/wecom/kf/callback", "1.2.3.4")
    assert status.startswith("Failed to connect") or status.startswith("exit:")


def test_callback_route_status_exception(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no curl")

    monkeypatch.setattr(cur.subprocess, "run", _boom)
    assert cur._callback_route_status("http://x", "1.2.3.4") == "error:OSError"


def test_callback_host_and_url_defaults_present():
    # 默认值锁定 IP 主机 + IP:80 回调路径，与企微后台当前配置一致。
    assert cur.CALLBACK_PROBE_HOST == "114.55.168.97"
    assert cur.CALLBACK_PROBE_URL == "http://127.0.0.1/wecom/kf/callback"
    assert "200" in cur.CALLBACK_HEALTHY_CODES and "422" in cur.CALLBACK_HEALTHY_CODES
