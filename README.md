# Git Sync PR

一个支持 GitHub、Gitee、AtomGit 的自动化 Cherry-pick 机器人，可自动从 PR 链接提取提交并应用到目标分支，支持命令行和 GitHub Actions 两种使用方式。

## ✨ 特性

- **多平台支持**：GitHub、Gitee、AtomGit
- **自动化流程**：解析 PR → 获取提交 → Cherry-pick → 推送 → 创建 PR
- **多种使用方式**：命令行本地执行 + GitHub Actions 远程执行
- **安全设计**：自动清理敏感信息，支持 Token 隐藏
- **灵活配置**：支持临时目录或现有仓库，可推送到个人 Fork 仓库
- **智能处理**：自动检测冲突，支持生成 Patch 文件

## 📦 安装

### 使用 uv（推荐）
```bash
# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 克隆项目
git clone https://github.com/Yiklek/git-sync-pr.git
cd git-sync-pr

# 安装依赖
uv sync
```

## 🚀 快速开始

### 命令行使用

#### 基本 Cherry-pick
```bash
# 从 PR cherry-pick 到 main 分支
git-sync-pr https://github.com/owner/repo/pull/123 --target-branch main

# 使用现有仓库
git-sync-pr https://github.com/owner/repo/pull/123 --target-branch main -r /path/to/repo
```

#### 推送到个人仓库并创建 PR
```bash
git-sync-pr https://github.com/owner/repo/pull/123 \
  --target-branch main \
  --personal-repo yourname/fork-repo \
  --create-pr \
  -t your_token
```

#### Gitee 平台
```bash
git-sync-pr https://gitee.com/owner/repo/pulls/123 --target-branch main
```

#### 生成 Patch 文件
```bash
# 生成单个 Patch 文件
git-sync-pr https://github.com/owner/repo/pull/123 --target-branch main --patch fix.patch

# 生成多个 Patch 文件到目录
git-sync-pr https://github.com/owner/repo/pull/123 --target-branch main --patch patches/
```

#### 测试运行（Dry-run）
```bash
git-sync-pr https://github.com/owner/repo/pull/123 --target-branch main --create-pr --dry-run
```

### GitHub Actions 使用

#### 1. 配置 Secrets
在仓库设置中（Settings → Secrets and variables → Actions）添加：
- `GITHUB_TOKEN`（GitHub 自动提供，已有）
- 或自定义 Token Secret（如 `MY_GITHUB_TOKEN`）

#### 2. 手动触发工作流
1. 进入仓库 Actions 页面
2. 选择 "Sync PR Bot" 工作流
3. 点击 "Run workflow"
4. 填写参数后执行

#### 3. 工作流参数说明

| 参数 | 必填 | 说明 | 示例 |
|------|------|------|------|
| `pr_url` | ✅ | PR 链接地址 | `https://github.com/owner/repo/pull/123` |
| `target_branch` | ❌ | 目标分支名称 | `main` |
| `personal_repo` | ❌ | 个人 Fork 仓库 | `yourname/fork-repo` |
| `patch_file` | ❌ | 生成 Patch 文件路径 | `fix.patch` 或 `patches/` |
| `create_pr` | ❌ | 是否自动创建 PR | `true` |
| `source_branch_name` | ❌ | 自定义源分支名 | `cherry-pick-123` |
| `title_prefix` | ❌ | PR 标题前缀 | `Backport:` |
| `body_tail` | ❌ | PR 描述尾部 | `This PR was created automatically. original PR: {pr_url}.` |
| `dry_run` | ❌ | 模拟运行模式 | `false` |
| `token_secret_name` | ❌ | Token Secret 名称 | `GITHUB_TOKEN` |


#### 4. 常用工作流配置

##### 基础 Cherry-pick
```yaml
pr_url: https://github.com/owner/repo/pull/123
target_branch: main
create_pr: true
token_secret_name: GITHUB_TOKEN
```

##### 推送到个人仓库
```yaml
pr_url: https://github.com/owner/repo/pull/123
target_branch: develop
personal_repo: yourname/fork-repo
create_pr: true
title_prefix: "Backport:"
token_secret_name: GITHUB_TOKEN
```

##### 生成 Patch 文件
```yaml
pr_url: https://github.com/owner/repo/pull/123
target_branch: main
patch_file: patches/
create_pr: false
token_secret_name: GITHUB_TOKEN
```

#### 5. 从命令行触发工作流

```bash
curl -L \
  -X POST \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  https://api.github.com/repos/OWNER/REPO/actions/workflows/sync-pr.yaml/dispatches \
  -d '{
    "ref": "master",
    "inputs": {
        "pr_url": "https://github.com/owner/repo/pull/123",
        "target_branch": "main",
        "create_pr": true,
        "token_secret_name": "TOKEN"
    }
  }'
```

## ⚙️ 配置说明

### Token 配置

#### GitHub Token
1. 访问 https://github.com/settings/tokens
2. 生成新 Token，勾选 `repo` 权限
3. 使用方式：
   ```bash
   # 命令行
   git-sync-pr https://github.com/owner/repo/pull/123 -t ghp_xxx

   # GitHub Actions（作为 Secret）
   # 在仓库 Settings → Secrets → Actions 添加 GITHUB_TOKEN
   ```

#### Gitee Token
1. 访问 https://gitee.com/profile/personal_access_tokens
2. 生成新 Token
3. 使用方式同上

### 本地仓库配置
```bash
# 使用 SSH 认证（推荐）
git config --global user.name "Your Name"
git config --global user.email "your.email@example.com"

# 验证 SSH 连接
ssh -T git@github.com
```

## 📁 项目结构

```
git-sync-pr/
├── .github/
│   └── workflows/
│       └── sync-pr.yaml   # GitHub Actions 工作流
├── main.py                # 主程序入口
├── pyproject.toml         # 项目配置和依赖
└── README.md              # 说明文档
```

## 🔧 开发

### 环境与构建
```bash
# 克隆仓库
git clone https://github.com/yourusername/git-sync-pr.git
cd git-sync-pr

# 安装开发依赖
uv sync --dev

# 代码格式化
uv run black .
uv run ruff check --fix .

# 构建项目
uv build
```


## ⚠️ 注意事项

1. **冲突处理**：遇到冲突时会自动中止，需要手动解决
2. **权限要求**：创建 PR 需要对应的 API 权限
3. **网络连接**：需要能够访问对应的 Git 平台 API

## 📄 许可证

本项目使用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。
