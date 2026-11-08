"""开发工具（git/deps/codex 委派）离线自检 + 可选在线端到端验证。

用法：
  python scripts/verify_codex_tools.py                # 离线检查（默认）
  python scripts/verify_codex_tools.py --online \\     # 在线端到端：真实 clone → codex → push
      --repo https://github.com/Mr-Shawn17/YuanAgent.git

在线前置：GitHub 代理开启、cc-switch 代理在跑（codex 模型）、
私有仓库先 `python -m runner set-git-cred github.com`。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from runner import coding as runner_coding  # noqa: E402
from runner import deps as runner_deps  # noqa: E402
from runner import gitops  # noqa: E402
from runner.config import RunnerConfig  # noqa: E402
from runner.executor import ALLOWED_TOOLS, execute_tool  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    suffix = f" — {detail}" if detail and not ok else ""
    print(f"[{mark}] {name}{suffix}")


def _expect_value_error(name: str, fn: Callable[..., object], *args: object) -> None:
    try:
        fn(*args)
    except ValueError:
        check(name, True)
        return
    check(name, False, "未抛 ValueError")


def offline_checks() -> int:
    print("== 离线检查（不联网、不跑 git/codex）==")
    config = RunnerConfig()

    # 1. 白名单
    check("执行器白名单含 4 个开发工具",
          {"git_clone", "git_commit_push", "install_deps", "delegate_coding"}
          <= ALLOWED_TOOLS)

    # 2. URL 校验
    url, host, name_ = gitops.parse_repo_url(
        "https://github.com/foo/Bar.git", ["github.com"])
    check("https+白名单主机解析", url.endswith("Bar.git") and host == "github.com"
          and name_ == "Bar")
    _expect_value_error("拒绝 http", gitops.parse_repo_url,
                        "http://github.com/foo/bar", ["github.com"])
    _expect_value_error("拒绝 ssh", gitops.parse_repo_url,
                        "git@github.com:foo/bar.git", ["github.com"])
    _expect_value_error("拒绝非白名单主机", gitops.parse_repo_url,
                        "https://gitee.com/foo/bar", ["github.com"])
    _expect_value_error("拒绝内嵌凭证", gitops.parse_repo_url,
                        "https://u:p@github.com/foo/bar", ["github.com"])
    _expect_value_error("拒绝注入字符", gitops.parse_repo_url,
                        'https://github.com/foo/bar && dir', ["github.com"])

    # 3. runner 侧工具参数错误返回可读文本（不抛异常）
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        out = execute_tool(workspace, "git_clone",
                           {"repo_url": "https://gitlab.com/a/b"}, config)
        check("runner 侧主机白名单拒绝", out.startswith("[参数无效]"))
        out = execute_tool(workspace, "delegate_coding", {"task": "x" * 9000}, config)
        check("task 超长拒绝", "过长" in out)
        out = execute_tool(workspace, "install_deps", {"manager": "bad"}, config)
        check("manager 非法拒绝", out.startswith("[参数无效]"))
        out = execute_tool(workspace, "git_commit_push", {"message": "m"}, config)
        check("未登记仓库推送拒绝", out.startswith("[未登记仓库]"))

    # 4. 依赖模板探测
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "requirements.txt").write_text("sympy\n", encoding="utf-8")
        template = runner_deps.detect_template(root, "auto")
        check("requirements.txt → pip 模板",
              template is not None and template[1][0] == "{python}")

    # 5. codex 检测（只探测，不执行）
    codex = runner_coding.detect_codex(config)
    check("codex CLI 探测", codex is not None, f"未找到 {config.codex.path}")

    # 6. 后端注册与档位
    from app.tools.builtin import BUILTIN_TOOL_SPECS
    check("四工具注册为 danger 档", all(
        name in BUILTIN_TOOL_SPECS and BUILTIN_TOOL_SPECS[name].default_tier == "danger"
        for name in ("git_clone", "git_commit_push", "install_deps", "delegate_coding")))

    # 7. tools.yaml 配置
    import yaml
    spec = yaml.safe_load((PROJECT_ROOT / "config" / "tools.yaml").read_text(encoding="utf-8"))
    tools = spec.get("tools", {})
    check("tools.yaml 四工具 enabled+tier=danger+mode=local", all(
        tools.get(name, {}).get("enabled") and tools.get(name, {}).get("tier") == "danger"
        and tools.get(name, {}).get("mode") == "local"
        for name in ("git_clone", "git_commit_push", "install_deps", "delegate_coding")))

    return FAIL


def _run(command: list[str], cwd: Path | None = None) -> int:
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print(proc.stdout[-800:])
        print(proc.stderr[-800:], file=sys.stderr)
    return proc.returncode


def online_checks(repo_url: str) -> int:
    print(f"== 在线端到端：{repo_url} ==")
    failures = 0
    if shutil.which("git") is None:
        print("[SKIP] 本机无 git")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        config = RunnerConfig()

        out = execute_tool(workspace, "git_clone", {"repo_url": repo_url}, config)
        ok = out.startswith("已克隆到") or out.startswith("已更新")
        check("git_clone 真实克隆", ok, out[:200])
        if not ok:
            return 1
        repo_name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
        check("clone 后存在 .yuan/remote.json 登记",
              (workspace / ".yuan" / "remote.json").is_file())

        out2 = execute_tool(workspace, "git_clone", {"repo_url": repo_url}, config)
        check("git_clone 幂等（二次执行走 pull）", "已更新" in out2 or "up to date" in out2
              or "Already up to date" in out2, out2[:200])

        task = f"请在仓库 {repo_name} 根目录创建文件 .yuan-codex-smoke.txt，内容为 ok"
        out3 = execute_tool(workspace, "delegate_coding", {"task": task}, config)
        check("delegate_coding 真实委派", not out3.startswith("[codex")
              and "退出码" not in out3, out3[:300])

        # 推送验证默认跳过（会污染真实仓库）；--push 显式开启
        if ARGS_PUSH:
            out4 = execute_tool(workspace, "git_commit_push",
                                {"message": "chore: codex 工具链冒烟测试"}, config)
            check("git_commit_push 真实推送", out4.startswith("已提交"), out4[:200])
        else:
            print("[SKIP] git_commit_push 推送（加 --push 显式开启）")
        failures = FAIL
    return failures


ARGS_PUSH = False


def main() -> int:
    global ARGS_PUSH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="执行在线端到端验证")
    parser.add_argument("--repo", default="https://github.com/Mr-Shawn17/YuanAgent.git")
    parser.add_argument("--push", action="store_true", help="在线模式同时验证真实推送")
    args = parser.parse_args()
    ARGS_PUSH = args.push

    failed = offline_checks()
    if failed:
        print(f"\n离线检查未通过（{failed} 项失败）")
        return 1
    if args.online:
        failed = online_checks(args.repo)
        if failed:
            print(f"\n在线验证未通过（{failed} 项失败）")
            return 1
    print(f"\n全部通过：{PASS} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
