use clap::Parser;
use regex::Regex;
use serde::Deserialize;
use std::env;
use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, ExitStatus, Output};
use std::str;
use tempfile::TempDir;
use thiserror::Error;

// 为Unix平台导入ExitStatusExt（处理ExitStatus::from_raw）
#[cfg(unix)]
use std::os::unix::process::ExitStatusExt;

/// Git平台枚举
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum GitPlatform {
    GitHub,
    Gitee,
    AtomGit,
}

impl GitPlatform {
    fn as_str(&self) -> &str {
        match self {
            GitPlatform::GitHub => "github",
            GitPlatform::Gitee => "gitee",
            GitPlatform::AtomGit => "atomgit",
        }
    }

    fn api_url_base(&self) -> &str {
        match self {
            GitPlatform::GitHub => "https://api.github.com",
            GitPlatform::Gitee => "https://gitee.com/api/v5",
            GitPlatform::AtomGit => "https://api.atomgit.com/api/v5",
        }
    }

    fn api_accept_header(&self) -> &str {
        match self {
            GitPlatform::GitHub => "application/vnd.github.v3+json",
            GitPlatform::Gitee => "application/json;charset=UTF-8",
            GitPlatform::AtomGit => "application/json;charset=UTF-8",
        }
    }

    fn remote_domain(&self) -> &str {
        match self {
            GitPlatform::GitHub => "github.com",
            GitPlatform::Gitee => "gitee.com",
            GitPlatform::AtomGit => "atomgit.com",
        }
    }
}

/// 自定义错误类型
#[derive(Error, Debug)]
enum CherryPickError {
    #[error("无效的PR URL格式: {0}")]
    InvalidPrUrl(String),

    #[error("API请求失败: {0}")]
    ApiRequestFailed(String),

    #[error("Git命令执行失败: {0}")]
    GitCommandFailed(String),

    #[error("文件系统操作失败: {0}")]
    FsOperationFailed(String),

    #[error("缺少必要的PR信息: {0}")]
    MissingPrInfo(String),

    #[error("用户取消操作")]
    UserCancelled,

    #[error("其他错误: {0}")]
    Other(String),
}

// 添加regex::Error的转换
impl From<regex::Error> for CherryPickError {
    fn from(e: regex::Error) -> Self {
        CherryPickError::Other(format!("正则表达式错误: {}", e))
    }
}

// 添加reqwest header错误的转换
impl From<reqwest::header::InvalidHeaderValue> for CherryPickError {
    fn from(e: reqwest::header::InvalidHeaderValue) -> Self {
        CherryPickError::Other(format!("Header值错误: {}", e))
    }
}

impl From<reqwest::Error> for CherryPickError {
    fn from(e: reqwest::Error) -> Self {
        CherryPickError::ApiRequestFailed(e.to_string())
    }
}

impl From<std::io::Error> for CherryPickError {
    fn from(e: std::io::Error) -> Self {
        CherryPickError::FsOperationFailed(e.to_string())
    }
}

impl From<std::str::Utf8Error> for CherryPickError {
    fn from(e: std::str::Utf8Error) -> Self {
        CherryPickError::Other(e.to_string())
    }
}

/// PR信息结构体
#[derive(Debug, Deserialize)]
struct PrInfo {
    #[serde(rename = "head")]
    head: PrRefInfo,
    #[serde(rename = "base")]
    base: PrRefInfo,
    #[serde(rename = "title")]
    title: Option<String>,
    #[serde(rename = "body")]
    body: Option<String>,
}

#[derive(Debug, Deserialize)]
struct PrRefInfo {
    #[serde(rename = "ref")]
    ref_name: String,
    #[serde(rename = "sha")]
    sha: String,
}

/// 创建PR请求体
#[derive(Debug, serde::Serialize)]
struct CreatePrRequest {
    title: String,
    body: String,
    head: String,
    base: String,
}

/// CherryPick机器人结构体
#[derive(Debug)]
struct CherryPickBot {
    token: Option<String>,
    dry_run: bool,
    auto_confirm: bool,
    platform: Option<GitPlatform>,
    repo_owner: Option<String>,
    repo_name: Option<String>,
    pr_number: Option<u32>,
    pr_url: String,
    target_repo: Option<String>,
    personal_repo: Option<String>,
    working_dir: PathBuf,
    is_temp_dir: bool,
    using_existing_repo: bool,
    source_remote_name: &'static str,
    personal_remote_name: &'static str,
}

impl CherryPickBot {
    /// 创建新的CherryPickBot实例
    fn new(
        token: Option<String>,
        dry_run: bool,
        auto_confirm: bool,
        pr_url: String,
        repo_path: Option<&str>,
    ) -> Result<Self, CherryPickError> {
        // 设置工作目录
        let (working_dir, is_temp_dir, using_existing_repo) = match repo_path {
            Some(path) => {
                let path_buf = PathBuf::from(path);
                if path_buf.exists() {
                    // 检查是否是Git仓库
                    let git_dir = path_buf.join(".git");
                    if git_dir.exists() && git_dir.is_dir() {
                        (path_buf, false, true)
                    } else {
                        // 创建临时目录 - 修复废弃的into_path
                        let temp_dir = TempDir::new()?;
                        let temp_path = temp_dir.path().to_path_buf();
                        // 保留临时目录（避免自动清理）
                        let _ = temp_dir.keep();

                        eprintln!(
                            "⚠️ 路径 '{}' 不是Git仓库，将在临时目录工作: {}",
                            path,
                            temp_path.display()
                        );
                        (temp_path, true, false)
                    }
                } else {
                    // 路径不存在，创建临时目录
                    let temp_dir = TempDir::new()?;
                    let temp_path = temp_dir.path().to_path_buf();
                    let _ = temp_dir.keep();

                    eprintln!(
                        "⚠️ 路径 '{}' 不存在，将在临时目录工作: {}",
                        path,
                        temp_path.display()
                    );
                    (temp_path, true, false)
                }
            }
            None => {
                // 创建临时目录
                let temp_dir = TempDir::new()?;
                let temp_path = temp_dir.path().to_path_buf();
                let _ = temp_dir.keep();
                (temp_path, true, false)
            }
        };

        Ok(Self {
            token,
            dry_run,
            auto_confirm,
            platform: None,
            repo_owner: None,
            repo_name: None,
            pr_number: None,
            pr_url,
            target_repo: None,
            personal_repo: None,
            working_dir,
            is_temp_dir,
            using_existing_repo,
            source_remote_name: "pr-source",
            personal_remote_name: "personal",
        })
    }

    /// 解析PR URL
    fn parse_pr_url(&mut self) -> Result<(), CherryPickError> {
        // 定义正则表达式 - 修复?操作符的错误转换
        let github_re = Regex::new(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)")?;
        let gitee_re = Regex::new(r"https?://gitee\.com/([^/]+)/([^/]+)/pulls/(\d+)")?;
        let atomgit_re = Regex::new(r"https?://atomgit\.com/([^/]+)/([^/]+)/pulls/(\d+)")?;

        // 匹配GitHub
        if let Some(captures) = github_re.captures(&self.pr_url) {
            self.platform = Some(GitPlatform::GitHub);
            self.repo_owner = Some(captures[1].to_string());
            self.repo_name = Some(captures[2].to_string());
            self.pr_number = Some(
                captures[3]
                    .parse()
                    .map_err(|e| CherryPickError::InvalidPrUrl(format!("无效的PR编号: {}", e)))?,
            );
            return Ok(());
        }

        // 匹配Gitee
        if let Some(captures) = gitee_re.captures(&self.pr_url) {
            self.platform = Some(GitPlatform::Gitee);
            self.repo_owner = Some(captures[1].to_string());
            self.repo_name = Some(captures[2].to_string());
            self.pr_number = Some(
                captures[3]
                    .parse()
                    .map_err(|e| CherryPickError::InvalidPrUrl(format!("无效的PR编号: {}", e)))?,
            );
            return Ok(());
        }

        // 匹配AtomGit
        if let Some(captures) = atomgit_re.captures(&self.pr_url) {
            self.platform = Some(GitPlatform::AtomGit);
            self.repo_owner = Some(captures[1].to_string());
            self.repo_name = Some(captures[2].to_string());
            self.pr_number = Some(
                captures[3]
                    .parse()
                    .map_err(|e| CherryPickError::InvalidPrUrl(format!("无效的PR编号: {}", e)))?,
            );
            return Ok(());
        }

        Err(CherryPickError::InvalidPrUrl(format!(
            "不支持的PR链接格式: {}",
            self.pr_url
        )))
    }

    /// 隐藏URL中的token
    fn hide_token_in_url(&self, url: &str) -> String {
        if let Some(token) = &self.token {
            url.replace(token, "[TOKEN_HIDDEN]")
        } else if url.starts_with("https://") && url.contains('@') {
            let parts: Vec<&str> = url.split('@').collect();
            if parts.len() == 2 {
                let protocol_part = parts[0].split("://").next().unwrap_or("https");
                format!("{}://[AUTH_HIDDEN]@{}", protocol_part, parts[1])
            } else {
                url.to_string()
            }
        } else {
            url.to_string()
        }
    }

    /// 运行Git命令
    fn run_git_command(&self, args: &[&str]) -> Result<Output, CherryPickError> {
        if self.dry_run {
            println!(
                "[DRY-RUN] 执行Git命令: git {} (工作目录: {})",
                args.join(" "),
                self.working_dir.display()
            );

            // 只读命令在dry-run模式下仍然执行
            let read_only_commands = [
                "log",
                "show",
                "ls-remote",
                "remote",
                "branch",
                "merge-base",
                "fetch",
                "clone",
                "status",
                "diff",
                "rev-parse",
                "symbolic-ref",
            ];

            let cmd = args.get(0).unwrap_or(&"");
            if read_only_commands.contains(cmd) {
                // 执行只读命令
                let output = Command::new("git")
                    .args(args)
                    .current_dir(&self.working_dir)
                    .output()?;

                if output.status.success() {
                    Ok(output)
                } else {
                    Err(CherryPickError::GitCommandFailed(format!(
                        "命令失败: git {}，错误: {}",
                        args.join(" "),
                        String::from_utf8_lossy(&output.stderr)
                    )))
                }
            } else {
                // 模拟成功输出 - 修复ExitStatus::from_raw
                #[cfg(unix)]
                let status = ExitStatus::from_raw(0);
                #[cfg(not(unix))]
                let status = ExitStatus::from(ExitCode::SUCCESS);

                Ok(Output {
                    status,
                    stdout: Vec::new(),
                    stderr: Vec::new(),
                })
            }
        } else {
            let output = Command::new("git")
                .args(args)
                .current_dir(&self.working_dir)
                .output()?;

            if output.status.success() {
                Ok(output)
            } else {
                Err(CherryPickError::GitCommandFailed(format!(
                    "命令失败: git {}，错误: {}",
                    args.join(" "),
                    String::from_utf8_lossy(&output.stderr)
                )))
            }
        }
    }

    /// 获取PR信息
    fn get_pr_info(&self) -> Result<PrInfo, CherryPickError> {
        let platform = self
            .platform
            .ok_or(CherryPickError::MissingPrInfo("未识别Git平台".to_string()))?;
        let owner = self
            .repo_owner
            .as_ref()
            .ok_or(CherryPickError::MissingPrInfo(
                "缺少仓库所有者信息".to_string(),
            ))?;
        let name = self
            .repo_name
            .as_ref()
            .ok_or(CherryPickError::MissingPrInfo(
                "缺少仓库名称信息".to_string(),
            ))?;
        let pr_num = self
            .pr_number
            .ok_or(CherryPickError::MissingPrInfo("缺少PR编号".to_string()))?;

        let api_url = format!(
            "{}/repos/{}/{}/pulls/{}",
            platform.api_url_base(),
            owner,
            name,
            pr_num
        );

        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(
            reqwest::header::ACCEPT,
            reqwest::header::HeaderValue::from_str(platform.api_accept_header())?,
        );
        headers.insert(
            reqwest::header::USER_AGENT,
            reqwest::header::HeaderValue::from_str(env!("CARGO_PKG_NAME"))?,
        );

        if let Some(token) = &self.token {
            headers.insert(
                reqwest::header::AUTHORIZATION,
                reqwest::header::HeaderValue::from_str(&format!("Bearer {}", token))?,
            );
        }

        let client = reqwest::blocking::Client::new();
        let response = client.get(&api_url).headers(headers).send()?;

        if !response.status().is_success() {
            return Err(CherryPickError::ApiRequestFailed(format!(
                "API请求返回错误状态码: {}，响应: {}",
                response.status(),
                response.text()?
            )));
        }

        let pr_info: PrInfo = response.json()?;
        Ok(pr_info)
    }

    /// 获取仓库远程URL
    fn get_repo_remote_url(
        &self,
        repo_full_name: &str,
        use_ssh: bool,
    ) -> Result<String, CherryPickError> {
        let platform = self
            .platform
            .ok_or(CherryPickError::Other("未识别Git平台".to_string()))?;
        let domain = platform.remote_domain();

        if use_ssh {
            Ok(format!("git@{}:{}.git", domain, repo_full_name))
        } else {
            if let Some(token) = &self.token {
                Ok(format!(
                    "https://oauth2:{}@{}/{}.git",
                    token, domain, repo_full_name
                ))
            } else {
                Ok(format!("https://{}/{}.git", domain, repo_full_name))
            }
        }
    }

    /// 设置远程仓库
    fn setup_remote(
        &mut self,
        remote_name: &str,
        repo_full_name: &str,
    ) -> Result<(), CherryPickError> {
        // 检查远程是否已存在
        let output = self.run_git_command(&["remote", "get-url", remote_name]);

        match output {
            Ok(output) => {
                let current_url = str::from_utf8(&output.stdout)?.trim();
                let expected_url = self.get_repo_remote_url(repo_full_name, false)?;

                if current_url != expected_url {
                    // 更新远程URL
                    self.run_git_command(&["remote", "set-url", remote_name, &expected_url])?;
                    println!(
                        "✅ 更新远程仓库: {} -> {}",
                        remote_name,
                        self.hide_token_in_url(&expected_url)
                    );
                } else {
                    println!("ℹ️ 远程仓库已存在: {}", remote_name);
                }
            }
            Err(_) => {
                // 添加新远程
                let https_url = self.get_repo_remote_url(repo_full_name, false)?;
                let result = self.run_git_command(&["remote", "add", remote_name, &https_url]);

                if result.is_err() {
                    // HTTPS失败，尝试SSH
                    println!("⚠️ HTTPS远程添加失败，尝试SSH...");
                    let ssh_url = self.get_repo_remote_url(repo_full_name, true)?;
                    self.run_git_command(&["remote", "add", remote_name, &ssh_url])?;
                    println!("✅ 已通过SSH添加远程仓库: {} -> {}", remote_name, ssh_url);
                } else {
                    println!(
                        "✅ 已添加远程仓库: {} -> {}",
                        remote_name,
                        self.hide_token_in_url(&https_url)
                    );
                }
            }
        }

        Ok(())
    }

    /// 克隆仓库
    fn clone_repo(&self, repo_full_name: &str) -> Result<(), CherryPickError> {
        if self.using_existing_repo {
            // 检查现有仓库配置
            println!("🔍 检查现有仓库配置...");
            let output = self.run_git_command(&["remote", "-v"])?;
            let remotes = str::from_utf8(&output.stdout)?;
            println!("📡 当前远程仓库配置:\n{}", self.hide_token_in_url(remotes));
            return Ok(());
        }

        // 克隆仓库
        let https_url = self.get_repo_remote_url(repo_full_name, false)?;
        println!(
            "🔧 克隆目标仓库: {} 到 {}",
            repo_full_name,
            self.working_dir.display()
        );
        println!("   仓库URL: {}", self.hide_token_in_url(&https_url));

        let result = self.run_git_command(&["clone", &https_url, "."]);

        if result.is_err() {
            // HTTPS克隆失败，尝试SSH
            println!("⚠️ HTTPS克隆失败，尝试SSH...");
            let ssh_url = self.get_repo_remote_url(repo_full_name, true)?;
            println!("🔧 尝试SSH URL: {}", ssh_url);
            self.run_git_command(&["clone", &ssh_url, "."])?;
            println!("✅ 成功通过SSH克隆仓库");
        } else {
            println!("✅ 成功克隆仓库");
        }

        Ok(())
    }

    /// 获取提交列表
    fn get_commits_between(
        &self,
        base_sha: &str,
        head_sha: &str,
    ) -> Result<Vec<String>, CherryPickError> {
        // 获取base commit
        self.run_git_command(&["fetch", self.source_remote_name, base_sha])?;
        // 获取head commit
        self.run_git_command(&["fetch", self.source_remote_name, head_sha])?;

        // 获取提交列表
        let range = format!("{}..{}", base_sha, head_sha);
        let output = self.run_git_command(&["log", "--pretty=format:%H", &range])?;
        let commits_str = str::from_utf8(&output.stdout)?;

        let mut commits: Vec<String> = commits_str
            .split('\n')
            .filter(|s| !s.is_empty())
            .map(|s| s.trim().to_string())
            .collect();

        if commits.is_empty() {
            return Err(CherryPickError::MissingPrInfo(format!(
                "在 {}..{} 中未找到提交",
                &base_sha[0..8],
                &head_sha[0..8]
            )));
        }

        // 反转顺序（按提交时间正序）
        commits.reverse();

        // 打印提交信息
        println!("📋 找到 {} 个提交:", commits.len());
        for (i, sha) in commits.iter().enumerate() {
            let output = self.run_git_command(&["show", "-s", "--format=%s", sha])?;
            let msg = str::from_utf8(&output.stdout)?.trim();
            println!("  {}. {} - {}", i + 1, &sha[0..8], msg);
        }

        Ok(commits)
    }
    /// 获取当前分支名称
    fn get_current_branch(&self) -> Result<Option<String>, CherryPickError> {
        let ret = self.run_git_command(&["symbolic-ref", "--short", "HEAD"]);
        match ret {
            Ok(output) => Ok(Some(str::from_utf8(&output.stdout)?.trim().to_string())),
            _ => Ok(None),
        }
    }
    /// 从git remote show <remote>输出中解析默认分支名（HEAD branch）
    fn get_remote_default_branch(&self, remote_name: &str) -> Result<String, CherryPickError> {
        // 执行git remote show <remote>命令
        let output = self.run_git_command(&["remote", "show", remote_name])?;
        let remote_show_output = str::from_utf8(&output.stdout)?.to_string();

        // 解析输出，找到"HEAD branch: xxx"这一行
        let default_branch_line = remote_show_output
            .lines()
            .find(|line| line.trim().starts_with("HEAD branch:"))
            .ok_or_else(|| {
                CherryPickError::GitCommandFailed(format!(
                    "无法从{}远程仓库的信息中解析默认分支名",
                    remote_name
                ))
            })?;

        // 提取分支名（去掉"HEAD branch: "前缀，清理空格）
        let default_branch = default_branch_line
            .split(':')
            .nth(1)
            .ok_or_else(|| {
                CherryPickError::GitCommandFailed(format!(
                    "无法从{}远程仓库的信息中解析默认分支名",
                    remote_name
                ))
            })?
            .trim()
            .to_string();

        if default_branch.is_empty() {
            return Err(CherryPickError::GitCommandFailed(format!(
                "{}远程仓库的默认分支名为空",
                remote_name
            )));
        }

        println!(
            "🔍 解析到{}远程仓库的默认分支名: {}",
            remote_name, default_branch
        );
        Ok(default_branch)
    }
    /// 切换到pr-source远程的主分支
    fn switch_to_pr_source_default_branch(&self) -> Result<(), CherryPickError> {
        // 第一步：拉取pr-source远程的最新分支信息
        println!("🔄 拉取pr-source远程仓库的最新分支信息...");
        self.run_git_command(&["fetch", self.source_remote_name])?;

        // 第二步：尝试切换到pr-source的默认分支
        let switch_main_result = self.run_git_command(&[
            "checkout",
            &format!(
                "{}/{}",
                self.source_remote_name,
                self.get_remote_default_branch(self.source_remote_name)?
            ),
        ]);

        if switch_main_result.is_ok() {
            println!(
                "✅ 已切换到本地临时分支: {}",
                format!("{}/main", self.source_remote_name),
            );
            return Ok(());
        }
        Ok(())
    }

    /// 删除分支（最终版：先切换到pr-source主分支再删除）
    fn delete_branch(&self, branch_name: &str) -> Result<(), CherryPickError> {
        // 步骤1：检查并切换分支（如果当前在待删除分支上）
        let current_branch = self.get_current_branch()?;
        if let Some(current) = current_branch {
            if current == branch_name {
                println!(
                    "⚠️ 当前正处于待删除分支 '{}'，切换到{}远程的主分支...",
                    branch_name, self.source_remote_name
                );
                // 切换到pr-source的主分支
                self.switch_to_pr_source_default_branch()?;
            }
        }

        // 步骤2：检查本地分支
        let output = self.run_git_command(&[
            "show-ref",
            "--verify",
            &format!("refs/heads/{}", branch_name),
        ]);

        if output.is_ok() {
            // 本地分支存在
            if !self.auto_confirm {
                print!("❓ 本地分支 '{}' 已存在，是否删除? (y/N) ", branch_name);
                std::io::stdout().flush()?;
                let mut input = String::new();
                std::io::stdin().read_line(&mut input)?;
                let input = input.trim().to_lowercase();

                if input != "y" {
                    return Err(CherryPickError::UserCancelled);
                }
            }

            // 删除本地分支
            println!("🗑️ 删除本地分支: {}", branch_name);
            self.run_git_command(&["branch", "-D", branch_name])?;
            println!("✅ 本地分支 '{}' 已删除", branch_name);
        } else {
            println!("ℹ️ 本地分支 '{}' 不存在，无需删除", branch_name);
        }

        // 步骤3：检查远程分支
        let remote = if self.personal_repo.is_some() {
            self.personal_remote_name
        } else {
            "origin"
        };

        let output = self.run_git_command(&["ls-remote", "--heads", remote, branch_name])?;
        if !output.stdout.is_empty() {
            // 远程分支存在
            if !self.auto_confirm {
                print!(
                    "❓ 远程分支 '{}/{}' 已存在，是否删除? (y/N) ",
                    remote, branch_name
                );
                std::io::stdout().flush()?;
                let mut input = String::new();
                std::io::stdin().read_line(&mut input)?;
                let input = input.trim().to_lowercase();

                if input != "y" {
                    return Err(CherryPickError::UserCancelled);
                }
            }

            // 删除远程分支
            println!("🗑️ 删除远程分支: {}/{}", remote, branch_name);
            self.run_git_command(&["push", remote, &format!(":{}", branch_name)])?;
            println!("✅ 远程分支 '{}/{}' 已删除", remote, branch_name);
        } else {
            println!("ℹ️ 远程分支 '{}/{}' 不存在，无需删除", remote, branch_name);
        }

        Ok(())
    }

    /// 创建分支
    fn create_branch(&self, branch_name: &str, base_branch: &str) -> Result<(), CherryPickError> {
        // 先删除现有分支
        if let Err(e) = self.delete_branch(branch_name) {
            if let CherryPickError::UserCancelled = e {
                return Err(e);
            }
            println!("⚠️ 删除分支失败: {}", e);
        }

        // 获取基础分支
        self.run_git_command(&["fetch", self.source_remote_name, base_branch])?;

        // 创建并切换到新分支
        let base_ref = format!("{}/{}", self.source_remote_name, base_branch);
        self.run_git_command(&["checkout", "-b", branch_name, &base_ref])?;
        println!(
            "✅ 已创建并切换到新分支: {} 基于 {}",
            branch_name, base_branch
        );

        Ok(())
    }

    /// 执行cherry-pick
    fn cherry_pick_commits(&self, commits: &[String]) -> Result<(), CherryPickError> {
        for (i, sha) in commits.iter().enumerate() {
            println!(
                "🍒 正在cherry-pick提交 {}/{}: {}",
                i + 1,
                commits.len(),
                &sha[0..8]
            );

            // 获取提交信息
            let output = self.run_git_command(&["show", "-s", "--format=%s", sha])?;
            let msg = str::from_utf8(&output.stdout)?.trim();
            println!("   提交信息: {}", msg);

            // 执行cherry-pick
            let result = self.run_git_command(&["cherry-pick", sha]);

            match result {
                Ok(_) => {
                    println!("  ✅ 提交 {} cherry-pick成功", &sha[0..8]);
                }
                Err(e) => {
                    println!("  ❌ 提交 {} cherry-pick失败: {}", &sha[0..8], e);

                    // 检查冲突
                    if let CherryPickError::GitCommandFailed(ref err_msg) = e {
                        if err_msg.to_lowercase().contains("conflict") {
                            println!("  ⚠️ 检测到冲突，正在中止cherry-pick...");
                            self.run_git_command(&["cherry-pick", "--abort"])?;
                            println!("  ✅ 已中止cherry-pick");

                            return Err(CherryPickError::GitCommandFailed(format!(
                                "cherry-pick冲突，提交 {} 需要手动解决",
                                &sha[0..8]
                            )));
                        }
                    }

                    return Err(e);
                }
            }
        }

        println!(
            "🎯 cherry-pick完成: 成功 {}/{} 个提交",
            commits.len(),
            commits.len()
        );
        Ok(())
    }

    /// 推送分支
    fn push_branch(&self, branch_name: &str) -> Result<(), CherryPickError> {
        // 确定推送的远程
        let (remote, remote_desc) = if let Some(personal_repo) = &self.personal_repo {
            (
                self.personal_remote_name,
                format!("个人仓库 ({})", personal_repo),
            )
        } else {
            (self.source_remote_name, "原始仓库".to_string())
        };

        println!("📤 推送更改到{}分支: {}", remote_desc, branch_name);

        // 执行推送
        let result = self.run_git_command(&["push", "--set-upstream", remote, branch_name]);

        if result.is_err() {
            // HTTPS推送失败，尝试SSH
            println!("⚠️ HTTPS推送失败，尝试SSH推送...");

            // 获取SSH URL
            let repo_full_name = if let Some(personal_repo) = &self.personal_repo {
                personal_repo
            } else {
                self.target_repo
                    .as_ref()
                    .ok_or_else(|| CherryPickError::Other("target_repo is None".into()))?
            };

            let ssh_url = self.get_repo_remote_url(repo_full_name, true)?;

            // 更新远程URL为SSH
            self.run_git_command(&["remote", "set-url", remote, &ssh_url])?;

            // 重新推送
            self.run_git_command(&["push", "--set-upstream", remote, branch_name])?;

            println!("✅ SSH推送成功: {}/{}", remote, branch_name);
        } else {
            println!("✅ 推送成功: {}/{}", remote, branch_name);
        }

        Ok(())
    }

    /// 创建PR
    fn create_pull_request(
        &self,
        target_repo: &str,
        target_branch: &str,
        source_branch: &str,
        pr_info: &PrInfo,
        title_prefix: Option<&str>,
        body_tail: Option<&str>,
    ) -> Result<(), CherryPickError> {
        let platform = self
            .platform
            .ok_or(CherryPickError::MissingPrInfo("未识别Git平台".to_string()))?;
        let pr_num = self
            .pr_number
            .ok_or(CherryPickError::MissingPrInfo("缺少PR编号".to_string()))?;

        // 构建标题
        let prefix = title_prefix.unwrap_or("Cherry-pick:");
        let title = pr_info
            .title
            .as_ref()
            .map(|t| format!("{} {}", prefix, t))
            .unwrap_or_else(|| format!("{} PR #{}", prefix, pr_num));

        // 构建描述
        let mut body = pr_info
            .body
            .as_ref()
            .map(|b| b.clone())
            .unwrap_or_else(|| format!("自动cherry-pick自 {}", self.pr_url));

        // 添加尾部信息
        if let Some(tail) = body_tail {
            let tail_formatted = tail
                .replace("{platform}", platform.as_str())
                .replace("{target_repo}", target_repo)
                .replace("{pr_number}", &pr_num.to_string())
                .replace("{pr_url}", &self.pr_url)
                .replace(
                    "{personal_repo}",
                    self.personal_repo
                        .as_ref()
                        .unwrap_or(&target_repo.to_string()),
                );

            body.push_str(&format!("\n\n{}", tail_formatted));
        }

        // 构建HEAD引用
        let head = if let Some(personal_repo) = &self.personal_repo {
            let parts: Vec<&str> = personal_repo.split('/').collect();
            if parts.len() == 2 {
                format!("{}:{}", parts[0], source_branch)
            } else {
                source_branch.to_string()
            }
        } else {
            source_branch.to_string()
        };

        // 创建PR请求
        let create_pr_req = CreatePrRequest {
            title,
            body,
            head,
            base: target_branch.to_string(),
        };

        // 发送创建PR请求
        let api_url = format!("{}/repos/{}/pulls", platform.api_url_base(), target_repo);

        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(
            reqwest::header::ACCEPT,
            reqwest::header::HeaderValue::from_str(platform.api_accept_header())?,
        );
        headers.insert(
            reqwest::header::USER_AGENT,
            reqwest::header::HeaderValue::from_str(env!("CARGO_PKG_NAME"))?,
        );

        if let Some(token) = &self.token {
            headers.insert(
                reqwest::header::AUTHORIZATION,
                reqwest::header::HeaderValue::from_str(&format!("Bearer {}", token))?,
            );
        }

        let client = reqwest::blocking::Client::new();
        let response = client
            .post(&api_url)
            .headers(headers)
            .json(&create_pr_req)
            .send()?;

        if !response.status().is_success() {
            return Err(CherryPickError::ApiRequestFailed(format!(
                "创建PR失败: {}，响应: {}",
                response.status(),
                response.text()?
            )));
        }

        let pr_response: serde_json::Value = response.json()?;
        let pr_url = pr_response["html_url"].as_str().unwrap_or("");
        println!("✅ PR创建成功: {}", pr_url);

        Ok(())
    }

    /// 生成patch文件 - 修复格式符d的错误
    fn generate_patch_file(
        &self,
        commits: &[String],
        patch_path: &str,
    ) -> Result<(), CherryPickError> {
        let patch_path_buf = PathBuf::from(patch_path);

        if patch_path_buf.is_dir() || patch_path.ends_with('/') || patch_path.ends_with('\\') {
            // 生成多个patch文件到目录
            let patch_dir = if patch_path_buf.is_dir() {
                patch_path_buf
            } else {
                fs::create_dir_all(&patch_path_buf)?;
                patch_path_buf
            };

            println!(
                "📁 将在目录中为每个提交生成单独的patch文件: {}",
                patch_dir.display()
            );

            for (i, sha) in commits.iter().enumerate() {
                // 获取提交信息
                let output = self.run_git_command(&["show", "-s", "--format=%s", sha])?;
                let msg = str::from_utf8(&output.stdout)?.trim();

                // 清理提交信息作为文件名 - 修复{:04d}为{:04}
                let sanitized_msg: String = msg
                    .chars()
                    .filter(|c| c.is_alphanumeric() || *c == '-' || *c == '_')
                    .take(50)
                    .collect();
                let sanitized_msg = sanitized_msg.replace(' ', "-");

                // 构建文件名 - 修复格式符d的错误
                let patch_filename = format!("{:04}-{}.patch", i + 1, sanitized_msg);
                let patch_file = patch_dir.join(patch_filename);

                // 生成patch
                let output = self.run_git_command(&["format-patch", "-1", "--stdout", sha])?;
                fs::write(&patch_file, &output.stdout)?;

                println!("  ✅ 已生成patch: {}", patch_file.display());
            }

            println!(
                "✅ 已为 {} 个提交生成patch文件到目录: {}",
                commits.len(),
                patch_dir.display()
            );
        } else {
            // 生成单个patch文件
            println!(
                "📄 生成包含所有提交的单个patch文件: {}",
                patch_path_buf.display()
            );

            let output = if commits.len() == 1 {
                self.run_git_command(&["format-patch", "-1", "--stdout", &commits[0]])?
            } else {
                let range = format!("{}^..{}", &commits[0], &commits[commits.len() - 1]);
                self.run_git_command(&["format-patch", &range, "--stdout"])?
            };

            fs::write(&patch_path_buf, &output.stdout)?;

            println!(
                "✅ 已生成patch文件: {} (大小: {} 字节)",
                patch_path_buf.display(),
                output.stdout.len()
            );
        }

        Ok(())
    }

    /// 清理敏感远程仓库
    fn cleanup_sensitive_remotes(&self) -> Result<(), CherryPickError> {
        if !self.using_existing_repo || self.dry_run {
            return Ok(());
        }

        println!("🔐 清理可能包含token的远程仓库...");

        let remotes_to_check = [self.source_remote_name, self.personal_remote_name];

        for remote in remotes_to_check {
            let output = self.run_git_command(&["remote", "get-url", remote]);

            if let Ok(output) = output {
                let remote_url = str::from_utf8(&output.stdout)?.trim();

                // 检查是否包含token
                if let Some(token) = &self.token {
                    if remote_url.contains(token) {
                        println!("⚠️ 检测到远程 '{}' 包含token，正在删除...", remote);
                        self.run_git_command(&["remote", "remove", remote])?;
                        println!("✅ 已删除远程仓库: {}", remote);
                    }
                } else if remote_url.starts_with("https://") && remote_url.contains('@') {
                    println!("⚠️ 检测到远程 '{}' 可能包含认证信息，正在删除...", remote);
                    self.run_git_command(&["remote", "remove", remote])?;
                    println!("✅ 已删除远程仓库: {}", remote);
                }
            }
        }

        Ok(())
    }

    /// 执行主流程 - 修复借用冲突
    fn run(
        &mut self,
        target_branch: &str,
        target_repo: Option<&str>,
        personal_repo: Option<&str>,
        create_pr: bool,
        source_branch_name: Option<&str>,
        title_prefix: Option<&str>,
        body_tail: Option<&str>,
        patch_file: Option<&str>,
    ) -> Result<(), CherryPickError> {
        // 打印启动信息
        println!("{}", "=".repeat(60));
        println!(
            "🤖 自动Cherry-pick机器人{}",
            if self.dry_run { " [DRY-RUN模式]" } else { "" }
        );
        if self.auto_confirm {
            println!("✅ 自动确认模式已启用");
        }
        if self.using_existing_repo {
            println!("🏠 使用现有仓库模式");
        }
        if let Some(prefix) = title_prefix {
            println!("📝 使用标题前缀: {}", prefix);
        }
        if let Some(tail) = body_tail {
            println!("📄 使用描述尾部: {}", &tail[0..tail.len().min(50)]);
        }
        if let Some(patch) = patch_file {
            println!("📁 将生成patch文件: {}", patch);
        }
        println!("{}", "=".repeat(60));

        // 1. 解析PR URL
        self.parse_pr_url()?;
        println!(
            "📋 PR信息: {}/{}/{}#{}",
            self.platform
                .ok_or_else(|| CherryPickError::Other("platform is None".into()))?
                .as_str(),
            self.repo_owner.as_ref().unwrap_or(&"".into()),
            self.repo_name.as_ref().unwrap_or(&"".into()),
            self.pr_number.unwrap_or(0)
        );

        // 2. 设置目标仓库
        self.target_repo = target_repo.map(|s| s.to_string()).or_else(|| {
            Some(format!(
                "{}/{}",
                self.repo_owner.as_ref().unwrap_or(&"".into()),
                self.repo_name.as_ref().unwrap_or(&"".into())
            ))
        });

        // 3. 设置个人仓库
        if let Some(pr) = personal_repo {
            self.personal_repo = Some(pr.to_string());
        }

        // 4. 克隆/检查仓库 - 修复借用冲突
        self.clone_repo(
            self.target_repo
                .as_ref()
                .ok_or_else(|| CherryPickError::Other("target_repo is None".into()))?,
        )?;

        // 5. 设置源远程
        let source_repo = format!(
            "{}/{}",
            self.repo_owner.as_ref().unwrap_or(&"".into()),
            self.repo_name.as_ref().unwrap_or(&"".into())
        );
        self.setup_remote(self.source_remote_name, &source_repo)?;

        // 6. 设置个人远程（如果有） - 修复借用冲突
        if let Some(personal_repo_clone) = self.personal_repo.clone() {
            self.setup_remote(self.personal_remote_name, &personal_repo_clone)?;
        }

        // 7. 获取PR信息
        let pr_info = self.get_pr_info()?;
        let head_sha = &pr_info.head.sha;
        let base_sha = &pr_info.base.sha;
        let head_ref = &pr_info.head.ref_name;
        let base_ref = &pr_info.base.ref_name;

        println!(
            "🔍 获取PR详细信息成功\n  Head分支: {} (commit: {})\n  Base分支: {} (commit: {})",
            head_ref,
            &head_sha[0..8],
            base_ref,
            &base_sha[0..8]
        );

        // 8. 获取提交列表
        let commits = self.get_commits_between(base_sha, head_sha)?;

        // 9. 生成patch文件（如果指定）
        if let Some(patch_path) = patch_file {
            self.generate_patch_file(&commits, patch_path)?;
        }

        // 10. 生成分支名
        let branch_name = if let Some(name) = source_branch_name {
            name.to_string()
        } else {
            let clean_target_branch = target_branch
                .replace(|c: char| !c.is_alphanumeric() && c != '-' && c != '_', "-")
                .replace('/', "-");

            format!(
                "cherry-pick-pr-{}-to-{}",
                self.pr_number.unwrap_or(0),
                clean_target_branch
            )
        };

        // 11. 创建分支
        self.create_branch(&branch_name, target_branch)?;

        // 12. 执行cherry-pick
        self.cherry_pick_commits(&commits)?;

        // 13. 推送分支
        self.push_branch(&branch_name)?;

        // 14. 创建PR（如果需要）
        if create_pr {
            self.create_pull_request(
                self.target_repo.as_ref().ok_or_else(|| CherryPickError::Other("target_repo is None".into()))?,
                target_branch,
                &branch_name,
                &pr_info,
                title_prefix,
                body_tail,
            )?;
        } else {
            if self.personal_repo.is_some() {
                println!(
                    "ℹ️ 自动推送完成，分支已推送到个人仓库: {}",
                    self.personal_repo.as_ref().ok_or_else(|| CherryPickError::Other("personal_repo is None".into()))?
                );
            } else {
                println!("ℹ️ 自动推送完成，分支: {}", branch_name);
            }
            println!("   如需创建PR，请使用: --create-pr 参数");
        }

        // 15. 清理敏感远程
        if self.using_existing_repo {
            self.cleanup_sensitive_remotes()?;
        }

        println!("\n{}", "=".repeat(60));
        println!(
            "🎉 Cherry-pick流程完成!{}",
            if self.dry_run {
                " [DRY-RUN模式未执行实际操作]"
            } else {
                ""
            }
        );
        if self.using_existing_repo {
            println!("ℹ️ 使用现有仓库，已清理可能包含token的远程仓库");
        }
        println!("{}", "=".repeat(60));

        Ok(())
    }

    /// 清理临时目录
    fn cleanup(&mut self) {
        if self.is_temp_dir && !self.dry_run {
            if let Err(e) = fs::remove_dir_all(&self.working_dir) {
                eprintln!("⚠️ 清理临时目录失败: {}", e);
            } else {
                println!("🧹 清理临时目录: {}", self.working_dir.display());
            }
        }
    }
}

impl Drop for CherryPickBot {
    fn drop(&mut self) {
        self.cleanup();
    }
}

/// 命令行参数解析
#[derive(Parser, Debug)]
#[command(author, version, about = "自动Cherry-pick机器人", long_about = None)]
struct Cli {
    /// PR链接地址 (GitHub或Gitee/AtomGit)
    pr_url: String,

    /// 目标分支名称
    #[arg(long, short = 'b')]
    target_branch: String,

    /// 本地Git仓库路径 (不指定则在临时目录中克隆)
    #[arg(long, short = 'r')]
    repo_path: Option<String>,

    /// API访问令牌 (Github Token或Gitee Token)
    #[arg(long, short = 't')]
    token: Option<String>,

    /// 从环境变量读取token的变量名 (如: GITHUB_TOKEN)
    #[arg(long)]
    token_env_var: Option<String>,

    /// 自动确认所有提示，无需手动输入
    #[arg(long, short = 'y')]
    yes: bool,

    /// 源分支名称 (默认自动生成)
    #[arg(long, short = 's')]
    source_branch_name: Option<String>,

    /// 目标仓库 (格式: owner/repo, 默认与源PR相同)
    #[arg(long)]
    target_repo: Option<String>,

    /// 个人仓库 (fork仓库) (格式: owner/repo)
    #[arg(long)]
    personal_repo: Option<String>,

    /// 自动创建PR
    #[arg(long)]
    create_pr: bool,

    /// PR标题前缀 (默认: 'Cherry-pick:')
    #[arg(long)]
    title_prefix: Option<String>,

    /// PR描述尾部，支持变量替换: {platform}, {target_repo}, {pr_number}, {pr_url}, {personal_repo}
    #[arg(long)]
    body_tail: Option<String>,

    /// 模拟运行，不执行实际操作
    #[arg(long)]
    dry_run: bool,

    /// 生成format-patch文件，可以是单个文件或目录
    #[arg(long, short = 'p')]
    patch: Option<String>,
}

fn main() -> Result<(), CherryPickError> {
    let cli = Cli::parse();

    // 处理token
    let token = if let Some(env_var) = &cli.token_env_var {
        env::var(env_var).ok().or(cli.token)
    } else {
        cli.token
    };

    if let Some(token) = &token {
        println!("✅ 获取token成功 (长度: {} 字符)", token.len());
    }

    // 创建机器人实例
    let mut bot = CherryPickBot::new(
        token,
        cli.dry_run,
        cli.yes,
        cli.pr_url,
        cli.repo_path.as_deref(),
    )?;

    // 执行主流程
    let result = bot.run(
        &cli.target_branch,
        cli.target_repo.as_deref(),
        cli.personal_repo.as_deref(),
        cli.create_pr,
        cli.source_branch_name.as_deref(),
        cli.title_prefix.as_deref(),
        cli.body_tail.as_deref(),
        cli.patch.as_deref(),
    );

    match result {
        Ok(_) => Ok(()),
        Err(e) => {
            eprintln!("❌ 执行失败: {}", e);
            std::process::exit(1);
        }
    }
}
