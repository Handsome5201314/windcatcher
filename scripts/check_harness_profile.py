"""Reject any DeepSeek Harness profile drift before an advisory model call.

This checks the effective sdk-minimal composition of the pinned upstream
0.2.2-alpha.2 runtime, including home/user overlays, not just our patch file.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path


EXPECTED_VERSION = "0.2.2-alpha.2"
EXPECTED_ROWS = dict(line.split(" ", 1) for line in """
sdk-app-startup @deepseek-ai/dsh-sdk-app
sdk-jsonrpc-server @deepseek-ai/dsh-sdk-jsonrpc-server
deepseek-llm-api-extensions @deepseek-ai/dsh-deepseek-llm-api-extensions
session-log-deepseek @deepseek-ai/dsh-session-log-deepseek
plugin-package-inventory-deepseek @deepseek-ai/dsh-plugin-package-inventory-deepseek
llm-deepseek @deepseek-ai/dsh-llm-deepseek-api-key
llm-pi-ai @deepseek-ai/dsh-llm-pi-ai
sandbox @deepseek-ai/dsh-sandbox-local
filesystem @deepseek-ai/dsh-fs-local
attachment-local @deepseek-ai/dsh-attachment-local
tool-fs @deepseek-ai/dsh-tool-fs
working-directory @deepseek-ai/dsh-working-directory
tool-working-directory @deepseek-ai/dsh-tool-working-directory
session-projection @deepseek-ai/dsh-session-projection
token-meter @deepseek-ai/dsh-token-meter
compaction-basic @deepseek-ai/dsh-compaction-basic
image-offload @deepseek-ai/dsh-compaction-image-offload
sandbox-policy @deepseek-ai/dsh-sandbox-policy
subprocess @deepseek-ai/dsh-subprocess-local
pty @deepseek-ai/dsh-terminal
terminal-bash @deepseek-ai/dsh-terminal-bash
terminal-pwsh @deepseek-ai/dsh-terminal-bash
timer @deepseek-ai/cordis-plugin-timer
llm @deepseek-ai/dsh-llm
session @deepseek-ai/dsh-session
session-title @deepseek-ai/dsh-session-title
system-prompt @deepseek-ai/dsh-system-prompt
tools @deepseek-ai/dsh-tools
tool-executor-native @deepseek-ai/dsh-tool-executor-native
mcp-resources @deepseek-ai/dsh-mcp-resources
agent @deepseek-ai/dsh-agent
llm-retry @deepseek-ai/dsh-llm-retry
jobs @deepseek-ai/dsh-jobs-local
agent-loop @deepseek-ai/dsh-agent-loop
persistent-bash @deepseek-ai/dsh-tool-bash-persistent
persistent-pwsh @deepseek-ai/dsh-tool-pwsh-persistent
sessions @deepseek-ai/dsh-session-persistence-jsonl
session-checkpoint-policy @deepseek-ai/dsh-session-checkpoint-policy
""".strip().splitlines())
DISABLED_ROWS = frozenset({
    "tool-fs", "tool-working-directory", "mcp-resources",
    "persistent-bash", "persistent-pwsh", "terminal-bash", "terminal-pwsh",
    "subprocess", "pty", "llm-deepseek",
})
_ROW = re.compile(r"(?m)^- id: ([A-Za-z0-9_-]+)[ \t]*$")
RELAY_ROW = (
    "  name: '@deepseek-ai/dsh-llm-pi-ai'",
    "  config:",
    "    providers:",
    "      windcatcher-relay:",
    "        apiKeyEnv: DEEPSEEK_API_KEY",
    "        api: openai-completions",
    "        baseURL: https://tongyimohe.aitoken.credit/v1",
    "        defaultContextWindow: 32768",
    "        defaultMaxTokens: 800",
    "        compat:",
    "          maxTokensField: max_completion_tokens",
    "        models:",
    "          - id: gpt-6.1-sol",
    "            name: gpt-6.1-sol",
)


class ProfileError(ValueError):
    """The effective Harness tree is not the audited tool-free composition."""


def check_composed_config(dump: str) -> None:
    if not isinstance(dump, str) or len(dump) > 100_000:
        raise ProfileError("Harness 配置输出无效")
    matches = list(_ROW.finditer(dump))
    rows = {}
    for index, match in enumerate(matches):
        row_id = match.group(1)
        if row_id in rows:
            raise ProfileError("Harness 配置存在重复条目")
        body = dump[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(dump)]
        name = re.search(r"(?m)^  name: ['\"]?([^'\"\r\n]+)['\"]?[ \t]*$", body)
        disabled = bool(re.search(r"(?m)^  disabled: true[ \t]*$", body))
        rows[row_id] = (name.group(1) if name else None, disabled, body)
    if set(rows) != set(EXPECTED_ROWS):
        raise ProfileError("Harness 配置条目与审计版本不符")
    for row_id, expected_name in EXPECTED_ROWS.items():
        name, disabled, _ = rows[row_id]
        if name != expected_name or disabled != (row_id in DISABLED_ROWS):
            raise ProfileError(f"Harness 配置条目不安全：{row_id}")
    policy = rows["sandbox-policy"][2]
    if not re.search(r"(?m)^    mode: read-only[ \t]*$", policy):
        raise ProfileError("Harness 文件策略不是 read-only")
    if not re.search(r"(?m)^    agents: \[\][ \t]*$", rows["agent-loop"][2]):
        raise ProfileError("Harness 不应启用子智能体")
    relay_lines = tuple(line.rstrip() for line in rows["llm-pi-ai"][2].splitlines()
                        if line.strip() and not line.lstrip().startswith("#"))
    if relay_lines != RELAY_ROW:
        raise ProfileError("Harness 中转站路由与审计版本不符")


def verify_runtime(dsh_bin: Path, patch: Path, home: Path) -> None:
    if not dsh_bin.is_file() or not patch.is_file():
        raise ProfileError("Harness 运行时或只读补丁不存在")
    home.mkdir(parents=True, exist_ok=True)
    allowed = {"systemroot", "windir", "path", "pathext", "comspec", "temp",
               "tmp", "userprofile", "appdata", "localappdata",
               "homedrive", "homepath", "programdata"}
    env = {key: value for key, value in os.environ.items() if key.lower() in allowed}
    env["DSH_HOME"] = str(home.resolve())
    try:
        version = subprocess.run([str(dsh_bin), "--version"], capture_output=True,
                                 text=True, timeout=15, env=env, check=True).stdout.strip()
        if version != EXPECTED_VERSION:
            raise ProfileError("Harness 运行时版本与审计版本不符")
        dump = subprocess.run([str(dsh_bin), "--profile", "sdk-minimal", "--patch",
                               str(patch.resolve()), "--dump-config"], capture_output=True,
                              text=True, timeout=20, env=env, check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProfileError(f"Harness 配置无法核验（{type(exc).__name__}）") from exc
    check_composed_config(dump)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsh-bin", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--patch", type=Path, default=Path(__file__).resolve().parents[1] /
                        "deploy" / "harness-readonly.patch.yml")
    args = parser.parse_args()
    try:
        verify_runtime(args.dsh_bin, args.patch, args.home)
    except ProfileError as exc:
        parser.exit(2, f"{exc}\n")
    print("Harness sdk-minimal profile: verified tool-free")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
