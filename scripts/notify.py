"""Create one GitHub issue per confirmed running instance, including retries."""
import hashlib
import json
import os
import subprocess


def api(path, payload=None):
    command = ["gh", "api", path]
    if payload is None:
        command += ["--paginate", "--slurp"]
    else:
        command += ["--method", "POST", "--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload) if payload else None,
                            text=True, capture_output=True, check=True, timeout=120)
    return json.loads(result.stdout)


def notify(environ=None, request=api):
    env = os.environ if environ is None else environ
    if (env.get("RESULT") not in {"created", "existing"}
            or env.get("STATE") != "RUNNING" or not env.get("INSTANCE_ID")):
        return None
    repo = env["GITHUB_REPOSITORY"]
    fingerprint = hashlib.sha256(env["INSTANCE_ID"].encode()).hexdigest()
    marker = f"<!-- oci-hunter-instance:{fingerprint} -->"
    # List all pages and both open/closed issues; search indexing can lag.
    pages = request(f"repos/{repo}/issues?state=all&per_page=100")
    for page in pages:
        for issue in page:
            if "pull_request" not in issue and marker in (issue.get("body") or ""):
                print(f"Already notified: {issue['html_url']}")
                return issue["html_url"]
    run_url = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{env['GITHUB_RUN_ID']}"
    body = (
        f"{marker}\n\n@{repo.split('/')[0]} 已确认抢到的机器处于 **RUNNING** 状态。\n\n"
        f"- 机型：`{env['SHAPE']}`\n"
        f"- 配置：{env.get('OCPUS', '?')} OCPU / {env.get('MEMORY_GB', '?')} GB\n"
        f"- 区域：`{env['REGION']}`\n"
        f"- [查看运行记录和实例详情]({run_url})\n\n"
        "请到 OCI 控制台确认公网 IP 和 SSH 登录。后续任务会查重，不会继续创建同名实例。\n"
        "此通知按实例去重；关闭 Issue 后也不会重复发送。"
    )
    issue = request(f"repos/{repo}/issues", {
        "title": f"已抢到机器：{env['SHAPE']} · {fingerprint[:8]}", "body": body,
    })
    print(f"Notification created: {issue['html_url']}")
    return issue["html_url"]


if __name__ == "__main__":
    notify()
