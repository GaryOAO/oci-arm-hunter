# OCI ARM (A1.Flex) capacity hunter

Oracle Cloud 的 `VM.Standard.A1.Flex`（ARM，永久免费 2 OCPU / 12 GB）在热门区域长期缺货，
手动点 Create 很难抢到释放出来的库存。这个仓库用 GitHub Actions 定时轮询，
一旦 AD-1 有容量就自动建机。

## 为什么用 GitHub Actions

- 不用一直开着电脑，云端定时跑
- cron 每 15 分钟触发一次
- 抢到就停（脚本会先检查实例是否已存在，避免重复创建）

## 需要配置的 Secrets / Variables

在仓库 **Settings → Secrets and variables → Actions** 里配置。

### Secrets（敏感，加密存储）

| 名称 | 说明 |
|---|---|
| `OCI_CLI_USER` | 用户 OCID |
| `OCI_CLI_TENANCY` | 租户 OCID |
| `OCI_CLI_FINGERPRINT` | API Key 指纹 |
| `OCI_API_PRIVATE_KEY` | API 私钥 PEM 全文（多行） |

### Variables（非敏感）

| 名称 | 值 |
|---|---|
| `OCI_CLI_REGION` | `us-sanjose-1` |
| `OCI_AD` | `pUWP:US-SANJOSE-1-AD-1` |
| `OCI_SUBNET_OCID` | 见下方 |
| `OCI_IMAGE_OCID` | 见下方 |
| `OCI_INSTANCE_NAME` | `ubuntu-arm-free` |
| `OCI_OCPUS` | `2` |
| `OCI_MEMORY_GB` | `12` |

### 本环境已确认的值

```
region      us-sanjose-1
AD          pUWP:US-SANJOSE-1-AD-1
IMAGE       Canonical-Ubuntu-24.04-aarch64（用 aarch64 版本才匹配 ARM 机型）
```

`OCI_SUBNET_OCID` 和 `OCI_IMAGE_OCID` 请从自己的控制台获取：

- **子网**：Networking → Virtual cloud networks → 你的 VCN → Subnets → 复制 OCID
- **镜像**：Compute → Instances → Create instance → 选好 Ubuntu 24.04 (aarch64) 后，
  从浏览器网络请求里取，或用 CLI：

  ```bash
  oci compute image list \
    --compartment-id <tenancy-ocid> \
    --operating-system "Canonical Ubuntu" \
    --shape VM.Standard.A1.Flex \
    --query "data[?contains(\"display-name\",'24.04')].{name:\"display-name\",id:id}" \
    --output table
  ```

## 手动触发

```bash
gh workflow run hunt.yml -f max_attempts=20 -f sleep_seconds=45
```

## 查看结果

```bash
gh run list --workflow=hunt.yml
gh run view <run-id> --log
```

抢到机器后，脚本输出里会有 `instance_id=ocid1.instance...`。

## 成本保护

- 只创建 `VM.Standard.A1.Flex`，且固定 `2 OCPU / 12 GB`（Always Free 上限）
- 启动卷用默认大小（46.6 GB，在 200 GB 免费额度内）
- 不加数据卷

> ⚠️ 账号处于 Free Trial 期间，Free Trial 结束后会降级为 Always Free。
> 只要不超额度就不会扣费，但仍建议在控制台设置**预算提醒**。
