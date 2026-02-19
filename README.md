# Docker Image Builder

为 R2E-Gym 和 SWE-bench Verified 数据集批量构建 Docker sandbox 镜像，并通过两步 F2P/P2P 验证确保镜像质量。

## 安装

```bash
uv sync
```

## 镜像命名规范

两个数据集采用统一的命名规范，默认 registry 为 `arl/`：

| 层级 | 命名格式 | 说明 |
|------|----------|------|
| Instance (最顶层) | `arl/{repo}_final:{hash/id}` | 每个 instance 一个，可直接运行测试 |
| Repo (仓库层) | `arl/{repo}_base:{hash}` | 每个 repo+version 共享，包含依赖环境 |
| Sys (系统层) | `base.py.{arch}:latest` | 仅 SWE-bench 使用，Ubuntu + Miniconda |

示例：

```
# R2E-Gym（2 层）
arl/aiohttp_base:latest
arl/aiohttp_final:f0d74880deec8fcd982bce639c93c5e130d41198

# SWE-bench Verified（3 层）
base.py.arm64:latest
arl/astropy_base:df8b7d77be804b314305d2
arl/astropy_final:d16bfe05ab0c697cdd7b4c37530eaa08e588a000
```

## 两步验证

两个数据集共用相同的验证逻辑，构建后自动运行：

| 步骤 | 操作 | F2P 预期 | P2P 预期 |
|------|------|----------|----------|
| Step 1 (pre-patch) | OLD commit 上跑测试 | FAIL | PASS |
| Step 2 (post-patch) | NEW commit / apply patch 后跑测试 | PASS | PASS |

两步都通过才算验证成功。验证不通过的镜像会被自动删除。每步执行都有超时保护（r2e 默认 300s，swe 默认 600s，均可通过 `--validation-timeout` 调整）。

## R2E-Gym（`r2e_docker`）

数据集：[R2E-Gym/R2E-Gym-Subset](https://huggingface.co/datasets/R2E-Gym/R2E-Gym-Subset)

每个 entry 对应一个 git commit（NEW/修复后 commit），构建 2 层镜像（repo base + instance）：

```bash
# 构建全部（base + commit + validation），默认 4 workers
HF_ENDPOINT=https://hf-mirror.com uv run python -m r2e_docker build_from_dataset \
  --registry pair-diag-cn-guangzhou.cr.volces.com/code/

# 只构建前 10 个，跳过 validation
uv run python -m r2e_docker build_from_dataset --limit 10 --no-validate

# 只构建 base images
uv run python -m r2e_docker build_from_dataset --base-only

# 自定义并发和 validation 超时
uv run python -m r2e_docker build_from_dataset --max-workers 8 --validation-timeout 600
```

单独验证：

```bash
# 简单验证（只检查退出码）
uv run python -m r2e_docker validate <image>

# F2P/P2P 验证（需要 expected_output_json）
uv run python -m r2e_docker validate <image> \
  --expected-output '{"Class.method": "PASSED", ...}'
```

## SWE-bench Verified（`swe_docker`）

数据集：[R2E-Gym/SWE-Bench-Verified](https://huggingface.co/datasets/R2E-Gym/SWE-Bench-Verified) · 独立于 `r2e_docker`，不依赖 swebench SDK

采用 3 层镜像架构（sys base → repo base → instance），最大化 Docker layer 复用。

支持的 12 个仓库：astropy, django, flask, matplotlib, pylint, pytest, requests, scikit-learn, seaborn, sphinx, sympy, xarray

```bash
# 构建全部（base + env + instance），默认 4 workers
HF_ENDPOINT=https://hf-mirror.com uv run python -m swe_docker build

# 构建前 5 个 + 两步验证
uv run python -m swe_docker build --limit 5 --validate

# 指定特定 instance
uv run python -m swe_docker build --instance-ids "django__django-12345,astropy__astropy-12907" --validate

# 自定义 registry 和并发
uv run python -m swe_docker build --registry myregistry/ --max-workers 8

# 强制重新构建
uv run python -m swe_docker build --limit 1 --force-rebuild --validate
```

单独验证：

```bash
uv run python -m swe_docker validate <image> --instance-id <instance_id>
```

## 输出日志

两个包的日志统一存放在 `output/` 目录下，默认只在失败时记录日志：

```
output/
├── r2e_docker/
│   └── failed_logs/
│       ├── base_{repo}.log
│       ├── commit_{key}.log
│       └── validation_{key}.log
└── swe_docker/
    ├── failed_logs/
    │   ├── base_{name}.log
    │   ├── env_{name}.log
    │   ├── instance_{id}.log
    │   └── validation_{id}.log
    └── build_logs/               # 仅 --verbose-logs 时生成
        ├── base/
        ├── env/
        └── instances/
```

swe_docker 支持 `--verbose-logs` 记录所有构建的详细日志（含 Dockerfile、脚本、构建输出）：

```bash
uv run python -m swe_docker build --limit 5 --verbose-logs
```

## 本地测试

从头跑通一个 instance 的完整 build + validate 流程（用于 debug 代码逻辑）：

```bash
# R2E-Gym
uv run python -m r2e_docker build_from_dataset --limit 1
# 若 Success，则已通过 F2P/P2P 检验
# 若 Fail，则在 output/r2e_docker/failed_logs/ 中记录失败原因

# SWE-bench Verified
uv run python -m swe_docker build --limit 1 --validate
# 成功会显示 Validation Passed=1, Failed=0
docker images | grep -E "base\.py\.|arl/"
```

## 项目结构

```
r2e_docker/                     # R2E-Gym 构建
├── config.py                   # DockerBuildConfig, RepoName, 测试命令
├── builder.py                  # Docker 构建逻辑（base + commit）
├── batch.py                    # 批量构建 + validation 集成
├── validator.py                # F2P/P2P 两步验证逻辑
├── cli.py                      # CLI 入口
├── dockerfiles/                # 各 repo 的 base Dockerfile
└── install_scripts/            # 各 repo 的安装脚本

swe_docker/                     # SWE-bench Verified 构建
├── constants.py                # 12 个 repo 的版本 specs, USE_X86, 测试命令
├── dockerfiles.py              # 3 层 Dockerfile 模板
├── scripts.py                  # 脚本生成（env setup, repo setup, eval）
├── builder.py                  # 3 层构建编排（sys base → repo base → instance）
├── validator.py                # 两步 F2P/P2P 验证
└── cli.py                      # Typer CLI 入口
```
