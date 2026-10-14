# OCI Always Free capacity hunter（ARM + x86）

目标是创建 **一台免费 Ubuntu 虚拟机**。默认轮流尝试：

| 机型 | 架构 | 配置 |
|---|---|---|
| `VM.Standard.A1.Flex` | ARM / aarch64 | 2 OCPU / 12 GB |
| `VM.Standard.E2.1.Micro` | AMD x86_64 | 固定 1/8 OCPU / 1 GB |

任意机型成功即停止创建。保留原来的实例名 `ubuntu-arm-free` 以兼容已有配置；这个名字现在也可能对应 x86。

## 为什么原来一直没有抢到

2026-10-14 检查运行日志时，最近抽查的 8 轮共 240 次尝试：198 次没有容量，42 次 429 限流。
脚本确实发出了创建请求。最新版本的 156 次定时运行，实际平均间隔约 20.3 分钟，
虽然原 cron 写的是每 5 分钟。每轮运行仅几分钟，留下较长的轮询空档。

新版每小时 `:17`、`:47` 触发，每轮最多轮询 55 分钟。任务串行排队，后来的任务不会取消正在抢机的任务。
默认两次请求之间至少等待 15 秒，并增加 0–20% 随机抖动；429 后提高间隔到 30–120 秒，
尊重数值形式的 `Retry-After`，随后逐渐恢复。GitHub 的定时触发仍可能延迟，库存也没有保证。

## 配置

在 **Settings → Secrets and variables → Actions** 配置。

### Secrets

| 名称 | 说明 |
|---|---|
| `OCI_CLI_USER` | 用户 OCID |
| `OCI_CLI_TENANCY` | 租户 OCID |
| `OCI_CLI_FINGERPRINT` | API Key 指纹 |
| `OCI_API_PRIVATE_KEY` | API 私钥 PEM 全文 |

### Variables

| 名称 | 默认值 / 说明 |
|---|---|
| `OCI_CLI_REGION` | `us-sanjose-1`；必须是租户主区域 |
| `OCI_AD` | `auto`；也可指定一个 AD，或逗号分隔多个 AD |
| `OCI_SUBNET_OCID` | 必填；已有公共子网 OCID |
| `OCI_COMPARTMENT_OCID` | 可选；默认在租户根 compartment 创建 |
| `OCI_IMAGE_OCID` | 可选；ARM Ubuntu 平台镜像，兼容原配置 |
| `OCI_X86_IMAGE_OCID` | 可选；x86 Ubuntu 平台镜像；未配置会自动选择兼容 E2.1.Micro 的最新 Ubuntu 22.04/24.04 镜像 |
| `OCI_SSH_PUBLIC_KEY` | SSH **公钥**全文；建议配置，否则不能通过普通 SSH 公钥登录 |
| `OCI_INSTANCE_NAME` | `ubuntu-arm-free` |
| `OCI_SHAPES` | `VM.Standard.A1.Flex,VM.Standard.E2.1.Micro`；顺序决定优先级，可仅填其中一种 |
| `OCI_OCPUS` | `2`；只影响 ARM，最大 2 |
| `OCI_MEMORY_GB` | `12`；只影响 ARM，最大 12 |

两种架构分别选择镜像，x86 不使用 ARM 的 `shape_config`。
指定的镜像会校验兼容机型，只接受无许可证费用的 Ubuntu 平台镜像。
已有配置无需添加 x86 镜像也能尝试 x86。

`auto` 会查询当前租户的 AD；AD 专属子网只会在所属 AD 创建。
San Jose 本身只有一个 AD，增加 AD-2/AD-3 无法增加当地库存。E2.1.Micro 在多 AD 区域只支持其中一个 AD，
脚本通过 `ListShapes` 过滤不支持的放置位置。

## 免费额度与查重

- 机型白名单只允许 A1.Flex 和 E2.1.Micro，拒绝付费 Flex 机型。
- 只在主区域创建；总 ARM 配置不超过 2 OCPU / 12 GB，总 Micro 数量不超过 2 台。
- 每台启动卷固定 50 GB、10 VPUs/GB；已有启动卷与块存储合计加新启动卷不得超过 200 GB。
- 枚举租户根目录和所有 ACTIVE 子 compartment，分页统计非 TERMINATED 资源；计入 STOPPED、PROVISIONING 和 TERMINATING 实例以及独立残留的启动卷。
- 任意 compartment 中已有同名实例，都会停止创建；实例清单或存储清单查询失败时停止。
- ARM 额度用满时仍可尝试 x86；x86 额度用满时仍可尝试 ARM。
- 不创建额外数据卷、备份或 Marketplace 资源。

API 用户需要能读取租户主区域、AD 和 compartment 清单，以及所有 compartment 中的实例、启动卷、块存储、镜像、机型和子网，
并在目标 compartment 创建实例/VNIC/启动卷。缺少清单读取权限时脚本会停止，避免在无法统计额度时创建。
检查与创建不是原子操作，运行期间请避免其他程序同时创建或扩容；已产生的历史用量和其他服务费用需要在 OCI 控制台核对。

## 请求超时与结果

请求提交前，脚本先保存配置摘要、AD、机型、时间和 `opc_retry_token` 到 `.hunt-state/request.json`。
如果请求超时或返回 5xx，继续检查实例清单，并使用**相同请求和 token**重试。
Actions cache 在任务间保存这份状态，不保存 API 私钥或 SSH 私钥。
如果未确认的请求配置发生变化或 token 接近 24 小时有效期，会停止并要求核查 OCI。
缓存是尽力保存的：任务被强制结束、缓存失效时无法保证保留跨任务状态，恢复前应核查实例。

实例创建请求被接受后最多等待 10 分钟，直到 RUNNING。
409 不会被直接当作成功；只有确认实例存在才算查重成功。
401/403/404、参数错误和未知错误使任务失败；容量不足、限流会单独计数。

Actions Summary 的 `result`：

| 值 | 含义 |
|---|---|
| `created` | 新实例已 RUNNING |
| `existing` | 已有同名 RUNNING 实例 |
| `pending_or_stopped` | 已确认实例存在，但仍在启动或已停止；不会继续创建 |
| `no_capacity` | 本轮尝试没有库存；任务可以为绿色，但不代表抢到了 |
| `throttled` | 本轮全部创建尝试都被限流 |
| `window_expired` | 运行窗口结束，尚未发出创建请求 |
| `unconfirmed` | 创建结果未确认，token 已保存；任务失败 |
| `quota_blocked` | 所有候选机型均触及额度/服务限制 |
| `instance_failed` | 接受请求后实例进入 TERMINATING/TERMINATED |
| `error` / `configuration_error` | 配置、权限、资源或其他错误；查看日志 |

## 手动运行与验证

短轮验证（两种机型各尝试一次）：

```bash
gh workflow run hunt.yml -f max_attempts=2 -f sleep_seconds=15 -f max_runtime_seconds=300
```

完整轮询窗口：

```bash
gh workflow run hunt.yml
gh run list --workflow=hunt.yml
gh run view <run-id> --log
```

本地测试不连接 OCI、不创建实例：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

已有实例时后续定时任务只查重并退出。抢到后可执行 `gh workflow disable hunt.yml` 关闭定时任务。

## 参考

实现参考了这些项目的思路，保留独立的 Python SDK 实现：

- [hitrov/oci-arm-host-capacity](https://github.com/hitrov/oci-arm-host-capacity)：非终止实例查重、持久化 429 等待状态。
- [filipbob/oracle-cloud-a1-capacity-retry](https://github.com/filipbob/oracle-cloud-a1-capacity-retry)：额度检查、随机抖动、退避、超时后的实例核查。

容量报告不预留容量，因此没有把报告结果作为创建请求的必要门槛。
免费额度和可用区域以 [Oracle Always Free 文档](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) 为准。
另见 [Oracle 区域/AD 文档](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm)
和 [GitHub schedule 说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。
