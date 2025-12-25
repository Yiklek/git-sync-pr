#!/usr/bin/env python3
"""
自动Cherry-pick机器人
支持Gitee和GitHub PR链接，自动cherry-pick到目标分支
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import shutil
from typing import Optional, Tuple, List, Dict
import requests


class GitPlatform:
    """Git平台枚举"""

    GITHUB = "github"
    GITEE = "gitee"
    ATOMGIT = "atomgit"


class CherryPickBot:
    def __init__(self, token: Optional[str] = None, dry_run: bool = False, auto_confirm: bool = False):
        self.token = token
        self.dry_run = dry_run
        self.auto_confirm = auto_confirm
        self.platform = None
        self.repo_owner = None
        self.repo_name = None
        self.target_repo = None
        self.pr_number = None
        self.pr_head_ref = None
        self.pr_base_ref = None
        self.pr_head_commit = None
        self.pr_base_commit = None
        self.source_remote_name = "pr-source"
        self.personal_repo = None
        self.personal_remote_name = "personal"
        self.working_dir = None
        self.is_temp_dir = False
        self.original_cwd = os.getcwd()
        self.using_existing_repo = False

    def __del__(self):
        """清理临时目录"""
        self.cleanup()

    def parse_pr_url(self, pr_url: str) -> Tuple[str, str, str, int]:
        """
        解析PR链接，提取平台、仓库所有者、仓库名和PR编号
        """
        github_pattern = r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)"
        gitee_pattern = r"https?://gitee\.com/([^/]+)/([^/]+)/pulls/(\d+)"
        atomgit_pattern = r"https?://atomgit\.com/([^/]+)/([^/]+)/pulls/(\d+)"

        github_match = re.match(github_pattern, pr_url)
        if github_match:
            self.platform = GitPlatform.GITHUB
            self.repo_owner, self.repo_name, pr_num = github_match.groups()
            self.pr_number = int(pr_num)
            return self.platform, self.repo_owner, self.repo_name, self.pr_number

        gitee_match = re.match(gitee_pattern, pr_url)
        if gitee_match:
            self.platform = GitPlatform.GITEE
            self.repo_owner, self.repo_name, pr_num = gitee_match.groups()
            self.pr_number = int(pr_num)
            return self.platform, self.repo_owner, self.repo_name, self.pr_number

        atomgit_match = re.match(atomgit_pattern, pr_url)
        if atomgit_match:
            self.platform = GitPlatform.ATOMGIT
            self.repo_owner, self.repo_name, pr_num = atomgit_match.groups()
            self.pr_number = int(pr_num)
            return self.platform, self.repo_owner, self.repo_name, self.pr_number

        raise ValueError(f"不支持的PR链接格式: {pr_url}")

    def _get_api_url_base(self, platform):
        url_base = {
            GitPlatform.GITHUB: "https://api.github.com",
            GitPlatform.GITEE: "https://gitee.com/api/v5",
            GitPlatform.ATOMGIT: "https://api.atomgit.com/api/v5",
        }
        url = url_base.get(platform)
        if url:
            url = url.rstrip("/")
        return url

    def _get_api_header_accept(self, platform):
        headers = {
            GitPlatform.GITHUB: "application/vnd.github.v3+json",
            GitPlatform.GITEE: "application/json;charset=UTF-8",
            GitPlatform.ATOMGIT: "application/json;charset=UTF-8",
        }
        return headers[platform]

    def _get_remote_domain(self, platform):
        domains = {
            GitPlatform.GITHUB: "github.com",
            GitPlatform.GITEE: "gitee.com",
            GitPlatform.ATOMGIT: "atomgit.com",
        }
        ret = domains.get(platform)
        if not ret:
            print(f"不支持的平台：{self.platform}")
        return ret

    def _get_repo_remote_url(self, platform, target_repo, token=None, http=False) -> Optional[str]:
        target_domain = self._get_remote_domain(platform)
        if not target_domain:
            return None

        scheme = "https"
        if http:
            scheme = "http"
        if token:
            repo_url = f"{scheme}://oauth2:{token}@{target_domain}/{target_repo}.git"
        else:
            repo_url = f"{scheme}://{target_domain}/{target_repo}.git"

        return repo_url

    def _get_repo_remote_ssh_url(self, platform, target_repo) -> Optional[str]:
        target_domain = self._get_remote_domain(platform)
        if not target_domain:
            return None

        return f"git@{target_domain}:{target_repo}.git"

    def cleanup(self):
        """清理资源"""
        if self.working_dir and self.is_temp_dir and os.path.exists(self.working_dir):
            if not self.dry_run:
                print(f"🧹 清理临时目录: {self.working_dir}")
                shutil.rmtree(self.working_dir, ignore_errors=True)

    def hide_token_in_url(self, url: str) -> str:
        """
        隐藏URL中的token信息，防止在日志中泄露
        返回安全的URL字符串
        """
        if not url:
            return url

        # 检查URL是否包含token
        if self.token and self.token in url:
            # 替换token为[TOKEN_HIDDEN]
            hidden_url = url.replace(self.token, "[TOKEN_HIDDEN]")
            return hidden_url

        # 检查是否是HTTPS URL且包含@符号（可能是认证信息）
        if url.startswith("https://") and "@" in url:
            # 格式: https://token@host/path
            parts = url.split("@", 1)
            if len(parts) == 2:
                prefix, suffix = parts
                # 替换@前面的部分
                if "://" in prefix:
                    protocol, _ = prefix.split("://", 1)
                    safe_url = f"{protocol}://[AUTH_HIDDEN]@{suffix}"
                else:
                    safe_url = f"[AUTH_HIDDEN]@{suffix}"
                return safe_url

        return url

    def remove_sensitive_remotes(self) -> bool:
        """
        删除可能包含token信息的远程仓库
        在使用现有仓库时特别重要，防止敏感信息泄露
        返回是否成功
        """
        if not self.using_existing_repo or self.dry_run:
            # 如果不是使用现有仓库，或者是在dry-run模式下，不需要删除
            if self.dry_run and self.using_existing_repo:
                print(f"[DRY-RUN] 将删除可能包含token的远程仓库")
            return True

        print("🔐 清理可能包含token的远程仓库...")

        try:
            # 检查当前目录是否是Git仓库
            if not os.path.exists(os.path.join(self.working_dir or str(), ".git")):
                print(f"⚠️ 当前目录不是Git仓库，无法清理远程仓库")
                return True

            # 切换到工作目录
            original_dir = os.getcwd()
            os.chdir(self.working_dir or str())

            try:
                # 定义需要检查的远程仓库名称
                remotes_to_check = [self.source_remote_name]
                if self.personal_remote_name:
                    remotes_to_check.append(self.personal_remote_name)

                for remote in remotes_to_check:
                    # 检查远程是否存在
                    result = subprocess.run(["git", "remote", "get-url", remote], capture_output=True, text=True)
                    if result.returncode == 0:
                        remote_url = result.stdout.strip()
                        # 检查URL是否包含token
                        if self.token and self.token in remote_url:
                            print(f"⚠️ 检测到远程 '{remote}' 包含token，正在删除...")
                            # 删除远程仓库
                            result = subprocess.run(["git", "remote", "remove", remote], capture_output=True, text=True)
                            if result.returncode == 0:
                                print(f"✅ 已删除远程仓库: {remote}")
                            else:
                                print(f"❌ 删除远程仓库失败: {result.stderr}")
                        elif remote_url.startswith("https://") and "@" in remote_url:
                            # URL包含@符号，可能是token或密码
                            print(f"⚠️ 检测到远程 '{remote}' 可能包含认证信息，正在删除...")
                            result = subprocess.run(["git", "remote", "remove", remote], capture_output=True, text=True)
                            if result.returncode == 0:
                                print(f"✅ 已删除远程仓库: {remote}")
                            else:
                                print(f"❌ 删除远程仓库失败: {result.stderr}")
            finally:
                # 切回原始目录
                os.chdir(original_dir)

            return True

        except Exception as e:
            print(f"❌ 清理远程仓库时发生错误: {e}")
            return True

    def get_pr_info_from_api(self) -> Dict:
        """
        通过API获取PR的详细信息，包括标题、描述、head和base分支
        使用Bearer认证
        """
        api_url = (
            f"{self._get_api_url_base(self.platform)}/repos/{self.repo_owner}/{self.repo_name}/pulls/{self.pr_number}"
        )
        headers = {"Accept": self._get_api_header_accept(self.platform)}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            response = requests.get(api_url, headers=headers)
            response.raise_for_status()
            pr_data = response.json()

            return pr_data

        except requests.RequestException as e:
            raise RuntimeError(f"API请求失败: {e}")

    def get_pr_info_from_api_extended(self) -> Dict:
        """
        通过API获取PR的扩展信息，包括commit SHA
        """
        pr_info = self.get_pr_info_from_api()

        head_ref = pr_info.get("head", {}).get("ref")
        base_ref = pr_info.get("base", {}).get("ref")
        head_commit_sha = pr_info.get("head", {}).get("sha")
        base_commit_sha = pr_info.get("base", {}).get("sha")

        if not all([head_ref, base_ref, head_commit_sha, base_commit_sha]):
            raise RuntimeError("从API获取的PR信息中缺少必要字段")

        return {
            "head_ref": head_ref,
            "base_ref": base_ref,
            "head_commit_sha": head_commit_sha,
            "base_commit_sha": base_commit_sha,
            "title": pr_info.get("title", ""),
            "body": pr_info.get("body", ""),
            "pr_info": pr_info,
        }

    def setup_working_directory(self, repo_path: Optional[str]) -> bool:
        """
        设置工作目录
        如果repo_path是None，则在临时目录中工作
        如果repo_path是Git仓库，则使用它
        否则在临时目录中工作
        """
        try:
            if self.dry_run:
                if repo_path is None:
                    print(f"[DRY-RUN] 将在临时目录中工作")
                else:
                    print(f"[DRY-RUN] 将设置工作目录: {repo_path}")
                return True

            # 如果repo_path是None，创建临时目录
            if repo_path is None:
                temp_dir = tempfile.mkdtemp(prefix="cherry_pick_")
                print(f"📁 创建临时工作目录: {temp_dir}")
                self.working_dir = temp_dir
                self.is_temp_dir = True
                self.using_existing_repo = False
                return True

            # 检查repo_path是否是有效的Git仓库
            repo_path_abs = os.path.abspath(repo_path)
            git_dir = os.path.join(repo_path_abs, ".git")

            if os.path.exists(git_dir):
                # 是现有Git仓库
                self.working_dir = repo_path_abs
                self.using_existing_repo = True
                self.is_temp_dir = False
                print(f"✅ 使用现有Git仓库: {self.working_dir}")
                return True
            elif os.path.exists(repo_path):
                # 路径存在但不是Git仓库
                print(f"⚠️ 路径 '{repo_path}' 不是Git仓库，将在临时目录中工作")

                # 创建临时目录
                temp_dir = tempfile.mkdtemp(prefix="cherry_pick_")
                print(f"📁 创建临时工作目录: {temp_dir}")
                self.working_dir = temp_dir
                self.is_temp_dir = True
                self.using_existing_repo = False
                return True
            else:
                # 路径不存在
                print(f"⚠️ 路径 '{repo_path}' 不存在，将在临时目录中工作")

                # 创建临时目录
                temp_dir = tempfile.mkdtemp(prefix="cherry_pick_")
                print(f"📁 创建临时工作目录: {temp_dir}")
                self.working_dir = temp_dir
                self.is_temp_dir = True
                self.using_existing_repo = False
                return True

        except Exception as e:
            print(f"❌ 设置工作目录时发生错误: {e}")
            return False

    def run_git_command(
        self, args: List[str], cwd: Optional[str] = None, capture_output: bool = True, env: Optional[Dict] = None
    ) -> subprocess.CompletedProcess:
        """
        运行git命令的通用方法
        在dry-run模式下，对于只读命令仍然执行以获取真实数据
        对于修改命令，只打印不执行
        """
        if cwd is None:
            cwd = self.working_dir

        if self.dry_run:
            cmd_str = " ".join(args)
            print(f"[DRY-RUN] 执行命令: {cmd_str}")
            if cwd:
                print(f"[DRY-RUN] 工作目录: {cwd}")

            # 定义只读命令列表
            read_only_commands = {
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
            }
            # 定义环境设置命令（在dry-run模式下应该执行的命令）
            setup_commands = {"clone", "remote", "fetch"}

            command = args[0] if len(args) > 0 else ""

            # 在dry-run模式下，对于只读命令和环境设置命令仍然执行
            if command in read_only_commands or command in setup_commands:
                try:
                    result = subprocess.run(args, cwd=cwd, capture_output=capture_output, text=True, env=env)
                    if result.returncode != 0 and command in setup_commands:
                        # 对于设置命令，即使失败也继续
                        print(f"[DRY-RUN] 命令执行可能失败: {result.stderr}")
                    return result
                except Exception as e:
                    print(f"[DRY-RUN] 执行命令时发生错误: {e}")
                    return subprocess.CompletedProcess(args, 1, "", str(e))
            else:
                # 对于修改命令，返回模拟结果
                return subprocess.CompletedProcess(args, 0, "", "")

        try:
            result = subprocess.run(args, cwd=cwd, capture_output=capture_output, text=True, env=env)
            return result
        except Exception as e:
            print(f"❌ 执行git命令失败: {' '.join(args)}")
            print(f"   错误: {e}")
            raise

    def check_existing_repo_alignment(self, target_repo: str) -> bool:
        """
        检查现有仓库是否与目标仓库对齐
        确保远程仓库正确设置，分支和commit与PR一致
        """
        if not self.using_existing_repo:
            return True

        print("🔍 检查现有仓库配置...")

        try:
            # 检查当前工作目录是否是Git仓库
            result = self.run_git_command(["git", "rev-parse", "--git-dir"])
            if result.returncode != 0:
                print(f"❌ 当前目录不是Git仓库: {result.stderr}")
                return False

            # 获取当前远程仓库信息
            result = self.run_git_command(["git", "remote", "-v"])
            if result.returncode != 0:
                print(f"❌ 无法获取远程仓库信息: {result.stderr}")
                return False

            print(f"📡 当前远程仓库配置:\n{self.hide_token_in_url(result.stdout)}")

            # 检查是否已经有目标仓库的远程
            result = self.run_git_command(["git", "remote", "get-url", "origin"])
            if result.returncode == 0:
                remote_url = result.stdout.strip()
                # 使用安全方式显示URL
                safe_url = self.hide_token_in_url(remote_url)
                print(f"✅ 已有远程仓库 origin: {safe_url}")

                # 检查远程URL是否与目标仓库匹配
                if target_repo not in remote_url:
                    print(f"⚠️ 现有仓库的远程origin与目标仓库不匹配")
                    print(f"   当前远程: {safe_url}")
                    print(f"   目标仓库: {target_repo}")
            else:
                print(f"⚠️ 当前仓库没有origin远程")

            # 检查当前分支
            result = self.run_git_command(["git", "branch", "--show-current"])
            if result.returncode == 0:
                current_branch = result.stdout.strip()
                print(f"🌿 当前分支: {current_branch}")

            # 获取当前提交
            result = self.run_git_command(["git", "log", "-1", "--pretty=format:%H %s"])
            if result.returncode == 0:
                current_commit = result.stdout.strip()
                print(f"📌 当前提交: {current_commit[:50]}...")

            return True

        except Exception as e:
            print(f"❌ 检查现有仓库配置时发生错误: {e}")
            return False

    def clone_or_init_repo(self, target_repo: str) -> bool:
        """
        克隆或初始化目标仓库
        支持使用token进行认证
        如果使用现有仓库，只检查配置
        """
        if self.using_existing_repo:
            # 使用现有仓库，检查对齐
            return self.check_existing_repo_alignment(target_repo)

        try:
            if self.dry_run:
                print(f"[DRY-RUN] 将克隆仓库: {target_repo} 到 {self.working_dir}")
                # 在dry-run模式下仍然尝试克隆，以便后续命令能工作
                pass

            repo_url = self._get_repo_remote_url(self.platform, target_repo, self.token)
            if not repo_url:
                return False

            # 使用安全方式显示URL
            safe_repo_url = self.hide_token_in_url(repo_url)
            print(f"🔧 克隆目标仓库: {target_repo}")
            print(f"   仓库URL: {safe_repo_url}")

            result = self.run_git_command(["git", "clone", repo_url, "."], cwd=self.working_dir)

            if result.returncode == 0:
                print(f"✅ 成功克隆仓库到: {self.working_dir}")
                return True
            else:
                print(f"❌ 克隆仓库失败: {result.stderr}")
                # 在dry-run模式下，即使克隆失败也继续
                if self.dry_run:
                    print(f"[DRY-RUN] 克隆失败，但在dry-run模式下继续...")
                    return True

                # 如果克隆失败，尝试使用SSH URL
                print(f"⚠️ HTTPS克隆失败，尝试使用SSH URL...")
                ssh_url = self._get_repo_remote_ssh_url(self.platform, target_repo)

                print(f"🔧 尝试SSH URL: {ssh_url}")
                result = self.run_git_command(["git", "clone", ssh_url or str(), "."], cwd=self.working_dir)

                if result.returncode == 0:
                    print(f"✅ 成功通过SSH克隆仓库")
                    return True
                else:
                    print(f"❌ SSH克隆也失败: {result.stderr}")
                    return False

        except Exception as e:
            print(f"❌ 克隆仓库时发生错误: {e}")
            if self.dry_run:
                print(f"[DRY-RUN] 错误发生，但在dry-run模式下继续...")
                return True
            return False

    def setup_remote(self, remote_name, platform, repo, token=None) -> bool:
        """
        设置仓库的远程URL
        支持使用token进行认证
        """
        try:
            remote_url = self._get_repo_remote_url(platform, repo, token)
            if not remote_url:
                return False

            if self.dry_run:
                print(f"[DRY-RUN] 将设置远程仓库: {remote_name}")
                # 使用安全方式显示URL
                safe_url = self.hide_token_in_url(remote_url)
                print(f"[DRY-RUN] 远程URL: {safe_url}")
                # 在dry-run模式下仍然尝试设置远程，以便后续命令能工作

            # 检查是否已存在远程仓库
            result = self.run_git_command(["git", "remote", "get-url", remote_name])

            if result.returncode != 0 or result.stdout != remote_url:
                remote_cmd = "add" if result.returncode != 0 else "set-url"
                # 添加远程仓库
                result = self.run_git_command(["git", "remote", remote_cmd, remote_name, remote_url])
                if result.returncode != 0:
                    print(f"❌ 添加远程仓库失败: {result.stderr}")

                    # 尝试使用SSH URL
                    print(f"⚠️ HTTPS远程添加失败，尝试SSH URL...")
                    ssh_url = self._get_repo_remote_ssh_url(platform, repo)
                    result = self.run_git_command(["git", "remote", remote_cmd, remote_name, ssh_url or str()])
                    if result.returncode == 0:
                        safe_ssh_url = ssh_url
                        print(f"✅ 已通过SSH添加远程仓库: {remote_name} -> {safe_ssh_url}")
                        return True

                    # 在dry-run模式下，即使失败也继续
                    if self.dry_run:
                        print(f"[DRY-RUN] 添加远程失败，但在dry-run模式下继续...")
                        return True
                    return False
                # 使用安全方式显示URL
                safe_url = self.hide_token_in_url(remote_url)
                print(f"✅ 已添加远程仓库: {remote_name} -> {safe_url}")
            else:
                print(f"ℹ️ 远程仓库已存在: {remote_name}")

            return True

        except Exception as e:
            print(f"❌ 设置远程仓库 {remote_name} 时发生错误: {e}")
            if self.dry_run:
                print(f"[DRY-RUN] 错误发生，但在dry-run模式下继续...")
                return True
            return False

    def setup_source_remote(self) -> bool:
        """
        设置PR源仓库的远程
        支持使用token进行认证
        """
        if not self.setup_remote(
            self.source_remote_name, self.platform, f"{self.repo_owner}/{self.repo_name}", self.token
        ):
            return False
        return True

    def setup_personal_remote(self) -> bool:
        """
        设置个人仓库的远程
        用于将分支推送到个人仓库（fork）
        """
        if not self.personal_repo:
            print("ℹ️ 未指定个人仓库，将推送到原始远程仓库")
            return True

        if not self.setup_remote(self.personal_remote_name, self.platform, self.personal_repo, self.token):
            return False
        return True

    def get_pr_branches_via_api(self) -> Tuple[str, str]:
        """
        通过API获取PR的head和base分支
        不使用默认值，直接从API获取真实的分支信息
        """
        try:
            if self.dry_run:
                print(f"[DRY-RUN] 将通过API获取PR分支信息")

            # 通过API获取PR信息
            pr_info = self.get_pr_info_from_api()

            # 从API响应中提取head和base分支
            head_ref = pr_info.get("head", {}).get("ref")
            base_ref = pr_info.get("base", {}).get("ref")

            if not head_ref or not base_ref:
                raise RuntimeError(f"从API获取的PR信息中缺少head或base分支信息")

            # 更新实例变量
            self.pr_head_ref = head_ref
            self.pr_base_ref = base_ref

            return head_ref, base_ref

        except Exception as e:
            raise RuntimeError(f"通过API获取PR分支失败: {e}")

    def get_commits_from_git(self, head_commit_sha: str, base_commit_sha: str) -> List[str]:
        """
        通过git命令获取两个commit之间的所有提交
        """
        try:
            if self.dry_run:
                print(f"[DRY-RUN] 将获取真实的提交信息")
            print(f"🔍 获取提交范围: {base_commit_sha[:8]}..{head_commit_sha[:8]}")

            result = self.run_git_command(["git", "fetch", self.source_remote_name, base_commit_sha])
            if result.returncode != 0:
                raise RuntimeError(f"无法获取base commit {base_commit_sha[:8]}")

            result = self.run_git_command(["git", "fetch", self.source_remote_name, head_commit_sha])
            if result.returncode != 0:
                raise RuntimeError(f"无法获取head commit {head_commit_sha[:8]}")

            result = self.run_git_command(["git", "log", "--pretty=format:%H", f"{base_commit_sha}..{head_commit_sha}"])
            if result.returncode != 0:
                raise RuntimeError(f"获取提交列表失败: {result.stderr}")

            commit_shas = [sha.strip() for sha in result.stdout.strip().split("\n") if sha.strip()]

            if not commit_shas:
                raise ValueError(f"在 {base_commit_sha[:8]}..{head_commit_sha[:8]} 中未找到新提交")

            if not commit_shas:
                if self.dry_run:
                    print(f"[DRY-RUN] 未找到新提交，使用模拟提交")
                    return ["a1b2c3d4e5f67890123456789abcdef012345678"]
                else:
                    raise ValueError(f"未找到新提交")

            commit_shas = list(reversed(commit_shas))

            print(f"📋 找到 {len(commit_shas)} 个提交:")
            for i, commit_sha in enumerate(commit_shas, 1):
                result = self.run_git_command(["git", "show", "-s", "--format=%s", commit_sha])
                if result.returncode == 0:
                    print(f"  {i}. {commit_sha[:8]} - {result.stdout.strip()}")
                else:
                    print(f"  {i}. {commit_sha[:8]} - (无法获取提交信息)")

            return commit_shas
        except Exception as e:
            if self.dry_run:
                print(f"[DRY-RUN] 获取提交失败，返回模拟提交: {e}")
                return ["a1b2c3d4e5f67890123456789abcdef012345678", "f1e2d3c4b5a67890123456789abcdef012345678"]
            else:
                raise RuntimeError(f"通过git获取提交失败: {e}")

    def delete_existing_branch(self, branch_name: str) -> bool:
        """
        删除已存在的分支（本地和远程）
        返回是否成功
        """
        if self.dry_run:
            print(f"[DRY-RUN] 将删除分支: {branch_name}")
            return True

        # 检查本地分支是否存在
        result = self.run_git_command(["git", "show-ref", "--verify", f"refs/heads/{branch_name}"])

        if result.returncode == 0:
            # 本地分支存在，询问是否删除
            if not self.auto_confirm:
                response = input(f"❓ 本地分支 '{branch_name}' 已存在，是否删除? (y/N): ").strip().lower()
                if response != "y":
                    print(f"❌ 用户取消删除本地分支 '{branch_name}'")
                    return False

            # 删除本地分支
            print(f"🗑️ 删除本地分支: {branch_name}")
            result = self.run_git_command(["git", "branch", "-D", branch_name])
            if result.returncode != 0:
                print(f"❌ 删除本地分支失败: {result.stderr}")
                return False
            print(f"✅ 本地分支 '{branch_name}' 已删除")

        # 检查并删除远程分支
        remote_to_check = "origin"
        if self.personal_repo:
            remote_to_check = self.personal_remote_name

        result = self.run_git_command(["git", "ls-remote", "--heads", remote_to_check, branch_name])

        if result.returncode == 0 and result.stdout.strip():
            # 远程分支存在，询问是否删除
            if not self.auto_confirm:
                response = (
                    input(f"❓ 远程分支 '{remote_to_check}/{branch_name}' 已存在，是否删除? (y/N): ").strip().lower()
                )
                if response != "y":
                    print(f"❌ 用户取消删除远程分支 '{remote_to_check}/{branch_name}'")
                    return False

            # 删除远程分支
            print(f"🗑️ 删除远程分支: {remote_to_check}/{branch_name}")
            result = self.run_git_command(["git", "push", remote_to_check, f":{branch_name}"])
            if result.returncode != 0:
                print(f"❌ 删除远程分支失败: {result.stderr}")
                return False
            print(f"✅ 远程分支 '{remote_to_check}/{branch_name}' 已删除")

        return True

    def create_branch_safe(self, branch_name: str, based_on: str) -> bool:
        """
        安全创建分支：检查分支是否存在，如果存在则删除
        返回是否成功
        """
        # 检查并删除已存在的分支
        if not self.delete_existing_branch(branch_name):
            return False

        # 现在创建新分支
        try:
            if self.dry_run:
                print(f"[DRY-RUN] 将创建分支: {branch_name} 基于 {based_on}")
                return True

            # 切换到基分支
            result = self.run_git_command(["git", "checkout", based_on])
            if result.returncode != 0:
                print(f"❌ 无法切换到基分支 {based_on}: {result.stderr}")
                return False

            # 创建新分支
            result = self.run_git_command(["git", "checkout", "-b", branch_name])
            if result.returncode == 0:
                print(f"✅ 已创建并切换到新分支: {branch_name}")
                return True
            else:
                print(f"❌ 创建分支失败: {result.stderr}")
                return False

        except Exception as e:
            print(f"❌ 创建分支时发生错误: {e}")
            return False

    def checkout_or_create_branch(self, branch: str, create_new: bool = False, based_on: Optional[str] = None) -> bool:
        """
        切换到目标分支或创建新分支
        如果分支不存在且create_new=True，则创建新分支
        可以指定新分支基于哪个分支创建（默认为当前分支）
        """
        try:
            if self.dry_run and create_new:
                print(f"[DRY-RUN] 将创建并切换到分支: {branch}")
                if based_on:
                    print(f"[DRY-RUN] 新分支将基于: {based_on}")
                return True
            elif self.dry_run:
                print(f"[DRY-RUN] 将切换到分支: {branch}")
                # 在dry-run模式下，我们仍然可以检查分支是否存在
                result = self.run_git_command(["git", "show-ref", "--verify", f"refs/heads/{branch}"])
                if result.returncode != 0:
                    print(f"[DRY-RUN] 分支 '{branch}' 不存在")
                return True

            # 检查分支是否存在
            result = self.run_git_command(["git", "show-ref", "--verify", f"refs/heads/{branch}"])

            if result.returncode != 0:
                if create_new:
                    # 检查远程是否有这个分支
                    remote_result = self.run_git_command(
                        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"]
                    )

                    if remote_result.returncode == 0 and remote_result.stdout.strip():
                        # 远程分支存在，创建本地分支并跟踪远程分支
                        result = self.run_git_command(["git", "checkout", "-b", branch, f"origin/{branch}"])
                        if result.returncode == 0:
                            print(f"✅ 已创建并切换到分支: {branch} (跟踪 origin/{branch})")
                        else:
                            print(f"❌ 创建分支失败: {result.stderr}")
                            return False
                    else:
                        # 创建新分支
                        # 如果指定了基分支，先切换到基分支
                        if based_on:
                            # 检查基分支是否存在
                            base_result = self.run_git_command(
                                ["git", "show-ref", "--verify", f"refs/heads/{based_on}"]
                            )
                            if base_result.returncode == 0:
                                # 切换到基分支
                                switch_result = self.run_git_command(["git", "checkout", based_on])
                                if switch_result.returncode != 0:
                                    print(f"⚠️ 无法切换到基分支 {based_on}，将使用当前分支")
                            else:
                                print(f"⚠️ 基分支 {based_on} 不存在，将使用当前分支")

                        # 创建新分支
                        result = self.run_git_command(["git", "checkout", "-b", branch])
                        if result.returncode == 0:
                            print(f"✅ 已创建并切换到新分支: {branch}")
                        else:
                            print(f"❌ 创建分支失败: {result.stderr}")
                            return False
                else:
                    # 如果create_new=False，但分支不存在，我们尝试创建一个基于默认分支的新分支
                    print(f"⚠️ 分支 '{branch}' 不存在，尝试创建新分支...")

                    # 获取默认分支
                    default_branch = "main"
                    result = self.run_git_command(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
                    if result.returncode == 0:
                        default_branch = result.stdout.strip().replace("origin/", "")

                    # 创建新分支
                    result = self.run_git_command(["git", "checkout", "-b", branch])
                    if result.returncode == 0:
                        print(f"✅ 已创建并切换到新分支: {branch} (基于 {default_branch})")
                    else:
                        print(f"❌ 创建分支失败: {result.stderr}")
                        return False
            else:
                # 切换到现有分支
                result = self.run_git_command(["git", "checkout", branch])
                if result.returncode == 0:
                    print(f"✅ 已切换到分支: {branch}")
                else:
                    print(f"❌ 切换分支失败: {result.stderr}")
                    return False

            # 只有在分支已存在且不是新创建的情况下才拉取最新代码
            if not create_new:
                result = self.run_git_command(["git", "pull", "origin", branch])
                if result.returncode != 0:
                    print(f"⚠️ 拉取分支 {branch} 最新代码失败: {result.stderr}")

            return True

        except Exception as e:
            print(f"❌ 分支操作时发生错误: {e}")
            return False

    def cherry_pick_commits(self, commit_shas: List[str]) -> bool:
        """
        按顺序cherry-pick多个提交
        返回是否成功，如果冲突则返回False
        """
        if not commit_shas:
            print("⚠️ 没有需要cherry-pick的提交")
            return False

        if self.dry_run:
            print(f"[DRY-RUN] 将cherry-pick以下真实的提交:")
            for i, sha in enumerate(commit_shas, 1):
                # 获取真实的提交信息
                result = self.run_git_command(["git", "show", "-s", "--format=%s", sha])
                commit_msg = result.stdout.strip() if result.returncode == 0 else "Unknown"
                print(f"  {i}. {sha[:8]} - {commit_msg}")
            return True

        try:
            success_count = 0
            failed_commits = []

            for i, commit_sha in enumerate(commit_shas, 1):
                print(f"🍒 正在cherry-pick提交 {i}/{len(commit_shas)}: {commit_sha[:8]}")

                # 获取提交信息
                result = self.run_git_command(["git", "show", "-s", "--format=%s", commit_sha])
                commit_msg = result.stdout.strip() if result.returncode == 0 else "Unknown"
                print(f"   提交信息: {commit_msg}")

                # 执行cherry-pick
                result = self.run_git_command(["git", "cherry-pick", commit_sha])

                if result.returncode == 0:
                    success_count += 1
                    print(f"  ✅ 提交 {commit_sha[:8]} cherry-pick成功")
                else:
                    error_msg = result.stderr
                    print(f"  ❌ 提交 {commit_sha[:8]} cherry-pick失败")
                    print(f"    错误信息: {error_msg[:200]}")
                    failed_commits.append((commit_sha, error_msg))

                    # 检查是否有冲突
                    if "conflict" in error_msg.lower():
                        print("  ⚠️ 检测到冲突，正在中止cherry-pick...")
                        abort_result = self.run_git_command(["git", "cherry-pick", "--abort"])
                        if abort_result.returncode == 0:
                            print("  ✅ 已中止cherry-pick")
                        else:
                            print(f"  ❌ 中止cherry-pick失败: {abort_result.stderr}")

                    # 询问是否继续
                    print(f"\n❌ cherry-pick冲突，无法继续。")
                    print(f"   请手动解决冲突后继续。")
                    print(f"   冲突提交: {commit_sha[:8]} - {commit_msg}")
                    return False

            if failed_commits:
                print(f"\n⚠️ 有 {len(failed_commits)} 个提交cherry-pick失败:")
                for sha, error in failed_commits:
                    print(f"  - {sha[:8]}: {error[:100]}...")

            print(f"🎯 cherry-pick完成: 成功 {success_count}/{len(commit_shas)} 个提交")
            return success_count > 0

        except KeyboardInterrupt:
            print("\n⏹️ 用户中断操作")
            return False
        except Exception as e:
            print(f"❌ cherry-pick过程中发生错误: {e}")
            return False

    def create_pull_request(
        self,
        target_repo: str,
        target_branch: str,
        source_branch: str,
        pr_info: Optional[Dict] = None,
        title_prefix: Optional[str] = None,
        body_tail: Optional[str] = None,
    ) -> bool:
        """
        自动创建PR，保持与源PR一致的标题和描述
        如果指定了个人仓库，则从个人仓库创建PR到目标仓库
        支持自定义标题前缀和描述尾部
        """
        if self.dry_run:
            print(f"[DRY-RUN] 将创建PR:")
            if self.personal_repo:
                print(f"  从个人仓库: {self.personal_repo}")
            print(f"  源分支: {source_branch}")
            print(f"  目标分支: {target_branch}")
            print(f"  目标仓库: {target_repo}")
            if title_prefix:
                print(f"  标题前缀: {title_prefix}")
            if body_tail:
                print(f"  描述尾部: {body_tail[:100]}...")
            if pr_info:
                print(f"  PR标题: {pr_info.get('title', '')}")
                print(f"  PR描述: {pr_info.get('body', '')[:200]}...")
            return True

        if not self.token:
            print("❌ 创建PR需要提供API token")
            return False

        # 如果提供了pr_info，使用其中的标题和描述
        pr_title = ""
        pr_body = ""

        if not pr_info:
            pr_info = self.get_pr_info_from_api()

        # 使用自定义标题前缀或默认前缀
        prefix = title_prefix or "Cherry-pick:"
        pr_title = f"{prefix} {pr_info.get('title', f'PR #{self.pr_number}')}"
        pr_body = pr_info.get("body", f"自动cherry-pick自 {self.pr_url}")

        # 添加cherry-pick说明
        cherry_pick_note = str()

        # 添加自定义描述尾部
        if body_tail:
            format_args = dict(
                platform=self.platform,
                target_repo=self.target_repo,
                pr_number=self.pr_number,
                personal_repo=self.personal_repo or self.target_repo,
                pr_url=self.pr_url,
            )
            print(self.pr_url)
            cherry_pick_note = f"\n\n{body_tail.format(**format_args)}"

        if len(pr_body) + len(cherry_pick_note) < 65536:  # GitHub PR body 最大长度
            pr_body += cherry_pick_note

        try:
            return self._create_platform_pr(
                self.platform or str(), target_repo, target_branch, source_branch, pr_title, pr_body
            )
        except Exception as e:
            print(f"❌ 创建PR时发生错误: {e}")
            return False

    def _create_platform_pr(
        self, platform: str, target_repo: str, target_branch: str, source_branch: str, pr_title: str, pr_body: str
    ) -> bool:
        """创建PR"""
        if self.dry_run:
            print(f"[DRY-RUN] 将创建 {platform} PR: {target_repo}")
            print(f"  标题: {pr_title}")
            print(f"  描述长度: {len(pr_body)} 字符")
            return True

        try:
            api_base = self._get_api_url_base(platform)
            api_url = f"{api_base}/repos/{target_repo}/pulls"
            headers = {"Accept": self._get_api_header_accept(platform), "Authorization": f"Bearer {self.token}"}

            # 如果指定了个人仓库，head应该为"个人仓库拥有者:分支名"
            if self.personal_repo:
                # 提取个人仓库的拥有者
                personal_owner = self.personal_repo.split("/")[0]
                head = f"{personal_owner}:{source_branch}"
                print(f"🔧 从个人仓库创建PR，head: {head}")
            else:
                head = source_branch

            data = {"title": pr_title, "body": pr_body, "head": head, "base": target_branch}

            print(f"📤 正在创建 {platform} PR...")
            print(f"  标题: {pr_title}")
            print(f"  描述长度: {len(pr_body)} 字符")

            response = requests.post(api_url, headers=headers, json=data)
            response.raise_for_status()

            pr_info = response.json()
            print(f"✅ PR创建成功: {pr_info['html_url']}")
            return True

        except requests.RequestException as e:
            print(f"❌ 创建 {platform} PR失败: {e}")
            if response.text:
                print(f"错误详情: {response.text}")
            return False

    def generate_patch_file(self, commit_shas: List[str], patch_file: str) -> bool:
        """
        生成patch文件
        """
        if not commit_shas:
            print("⚠️ 没有需要生成patch的提交")
            return False

        if self.dry_run:
            print(f"[DRY-RUN] 将为 {len(commit_shas)} 个提交生成patch文件: {patch_file}")
            for i, sha in enumerate(commit_shas, 1):
                result = self.run_git_command(["git", "show", "-s", "--format=%s", sha])
                commit_msg = result.stdout.strip() if result.returncode == 0 else "Unknown"
                print(f"  {i}. {sha[:8]} - {commit_msg}")
            return True

        try:
            print(f"📁 为 {len(commit_shas)} 个提交生成patch文件: {patch_file}")

            patch_dir = os.path.dirname(patch_file)
            if patch_dir and not os.path.exists(patch_dir):
                os.makedirs(patch_dir, exist_ok=True)

            if os.path.isdir(patch_file) or patch_file.endswith("/") or patch_file.endswith("\\"):
                patch_dir = patch_file.rstrip("/").rstrip("\\")
                if not os.path.exists(patch_dir):
                    os.makedirs(patch_dir, exist_ok=True)

                print(f"📁 将在目录中为每个提交生成单独的patch文件: {patch_dir}")

                for i, commit_sha in enumerate(commit_shas, 1):
                    result = self.run_git_command(["git", "show", "-s", "--format=%s", commit_sha])
                    commit_msg = result.stdout.strip() if result.returncode == 0 else "Unknown"
                    print(f"📄 为提交 {i}/{len(commit_shas)} 生成patch: {commit_sha[:8]} - {commit_msg}")

                    patch_num = f"{i:04d}"
                    sanitized_msg = re.sub(r"[^\w\s-]", "", commit_msg)[:50]
                    sanitized_msg = re.sub(r"[-\s]+", "-", sanitized_msg)
                    single_patch = os.path.join(patch_dir, f"{patch_num}-{sanitized_msg}.patch")

                    result = self.run_git_command(["git", "format-patch", "-1", "--stdout", commit_sha])
                    if result.returncode == 0:
                        with open(single_patch, "w", encoding="utf-8") as f:
                            f.write(result.stdout)
                        print(f"  ✅ 已生成patch: {single_patch}")
                    else:
                        print(f"  ❌ 生成patch失败: {result.stderr}")
                        return False

                print(f"✅ 已为 {len(commit_shas)} 个提交生成patch文件到目录: {patch_dir}")
                return True
            else:
                print(f"📄 生成包含所有提交的单个patch文件: {patch_file}")

                if len(commit_shas) == 1:
                    result = self.run_git_command(["git", "format-patch", "-1", "--stdout", commit_shas[0]])
                else:
                    result = self.run_git_command(
                        ["git", "format-patch", f"{commit_shas[0]}^..{commit_shas[-1]}", "--stdout"]
                    )

                if result.returncode == 0:
                    with open(patch_file, "w", encoding="utf-8") as f:
                        f.write(result.stdout)
                    print(f"✅ 已生成patch文件: {patch_file}")
                    print(f"   文件大小: {len(result.stdout)} 字节")
                    return True
                else:
                    print(f"❌ 生成patch失败: {result.stderr}")
                    return False
        except Exception as e:
            print(f"❌ 生成patch文件时发生错误: {e}")
            return False

    def push_changes(self, branch: str) -> bool:
        """
        推送更改到远程仓库
        如果指定了个人仓库，则推送到个人仓库
        否则推送到origin
        """
        if self.dry_run:
            if self.personal_repo:
                print(f"[DRY-RUN] 将推送分支 {branch} 到个人仓库: {self.personal_repo}")
            else:
                print(f"[DRY-RUN] 将推送分支 {branch} 到原始仓库")
            return True

        try:
            # 确定推送到哪个远程
            if self.personal_repo:
                remote = self.personal_remote_name
                remote_name = f"个人仓库 ({self.personal_repo})"
            else:
                remote = self.source_remote_name
                remote_name = "原始仓库"

            print(f"📤 推送更改到{remote_name}分支: {branch}")

            # 执行推送
            result = self.run_git_command(["git", "push", "--set-upstream", remote, branch])

            if result.returncode == 0:
                print(f"✅ 推送成功: {remote}/{branch}")
                return True
            else:
                error_msg = result.stderr
                print(f"❌ 推送到{remote_name}失败: {error_msg}")

                # 尝试使用SSH推送
                print(f"⚠️ HTTPS推送失败，尝试使用SSH推送...")

                # 检查是否配置了SSH密钥
                ssh_test_cmd = ["ssh", "-T", f"git@{self._get_remote_domain(self.platform)}"]
                ssh_result = self.run_git_command(ssh_test_cmd, capture_output=True)
                if ssh_result.returncode == 1 and "successfully authenticated" in ssh_result.stderr.lower():
                    print(f"✅ SSH密钥配置正确，尝试SSH推送")

                    # 获取当前远程URL
                    result = self.run_git_command(["git", "remote", "get-url", remote])
                    if result.returncode == 0:
                        # 如果是HTTPS URL，转换为SSH URL
                        ssh_url = self._get_repo_remote_ssh_url(self.platform, self.personal_repo or self.target_repo)
                        # 设置SSH远程URL
                        self.run_git_command(["git", "remote", "set-url", remote, ssh_url or str()])
                        print(f"✅ 已设置为SSH远程: {ssh_url}")

                    # 重新尝试推送
                    result = self.run_git_command(["git", "push", "--set-upstream", remote, branch])
                    if result.returncode == 0:
                        print(f"✅ SSH推送成功: {remote}/{branch}")
                        return True
                    else:
                        print(f"❌ SSH推送也失败: {result.stderr}")
                else:
                    print(f"⚠️ SSH密钥未配置或配置不正确")
                    print(f"   错误信息: {ssh_result.stderr if ssh_result.stderr else ssh_result.stdout}")

                return False

        except Exception as e:
            print(f"❌ 推送过程中发生错误: {e}")
            return False

    def run(
        self,
        pr_url: str,
        target_branch: str,
        repo_path: Optional[str] = None,
        target_repo: Optional[str] = None,
        personal_repo: Optional[str] = None,
        create_pr: bool = False,
        source_branch_name: Optional[str] = None,
        token: Optional[str] = None,
        title_prefix: Optional[str] = None,
        body_tail: Optional[str] = None,
        patch_file: Optional[str] = None,
    ) -> bool:
        """
        执行完整的cherry-pick流程
        """
        if token:
            self.token = token

        print("=" * 60)
        print("🤖 自动Cherry-pick机器人" + (" [DRY-RUN模式]" if self.dry_run else ""))
        if self.auto_confirm:
            print("✅ 自动确认模式已启用")
        if self.using_existing_repo:
            print("🏠 使用现有仓库模式")
        if title_prefix:
            print(f"📝 使用标题前缀: {title_prefix}")
        if body_tail:
            print(f"📄 使用描述尾部: {body_tail[:50]}...")
        if patch_file:
            print(f"📁 将生成patch文件: {patch_file}")
        print("=" * 60)

        try:
            # 1. 设置工作目录
            if not self.setup_working_directory(repo_path):
                print("❌ 设置工作目录失败")
                return False

            print(f"📁 工作目录: {self.working_dir}")

            # 2. 解析PR链接
            try:
                self.pr_url = pr_url
                platform, owner, repo, pr_num = self.parse_pr_url(pr_url)
                print(f"📋 PR信息: {platform}/{owner}/{repo}#{pr_num}")
            except ValueError as e:
                print(f"❌ 解析PR链接失败: {e}")
                return False

            # 3. 克隆或初始化仓库
            self.target_repo = target_repo or f"{owner}/{repo}"
            if not self.clone_or_init_repo(self.target_repo):
                print("❌ 克隆/初始化仓库失败")
                return False

            # 4. 设置源仓库远程
            if not self.setup_source_remote():
                print("❌ 设置源仓库远程失败")
                return False

            # 5. 设置个人仓库远程（如果指定了个人仓库）
            if personal_repo:
                self.personal_repo = personal_repo
                if not self.setup_personal_remote():
                    print("⚠️ 设置个人仓库远程失败，将继续使用原始仓库")
                    self.personal_repo = None

            # 6. 通过API获取PR详细信息（包括分支名和commit SHA）
            try:
                pr_info_extended = self.get_pr_info_from_api_extended()
                head_ref = pr_info_extended["head_ref"]
                base_ref = pr_info_extended["base_ref"]
                head_commit_sha = pr_info_extended["head_commit_sha"]
                base_commit_sha = pr_info_extended["base_commit_sha"]

                # 更新实例变量
                self.pr_head_ref = head_ref
                self.pr_base_ref = base_ref
                self.pr_head_commit = head_commit_sha
                self.pr_base_commit = base_commit_sha

                print(f"🔍 获取PR详细信息成功")
                print(f"  Head分支: {head_ref} (commit: {head_commit_sha[:8]})")
                print(f"  Base分支: {base_ref} (commit: {base_commit_sha[:8]})")

            except Exception as e:
                print(f"❌ 获取PR详细信息失败: {e}")
                return False

            # 7. 通过git命令获取提交信息（基于commit SHA）
            try:
                commit_shas = self.get_commits_from_git(head_commit_sha, base_commit_sha)
                if not commit_shas:
                    print("❌ 未找到有效的提交信息")
                    return False
            except Exception as e:
                print(f"❌ 获取提交信息失败: {e}")
                return False

            # 8. 如果指定了patch_file，生成patch文件
            if patch_file:
                if not self.generate_patch_file(commit_shas, patch_file):
                    print("❌ 生成patch文件失败")
                    return False

                print("\n" + "=" * 60)
                print("🎉 Patch文件生成完成!" + (" [DRY-RUN模式未执行实际操作]" if self.dry_run else ""))
                print("=" * 60)

            # 9. 获取源PR信息（用于创建PR时复制）
            pr_info = None
            if create_pr:
                try:
                    pr_info = self.get_pr_info_from_api()
                    print(f"📄 获取源PR信息成功")
                    print(f"  标题: {pr_info.get('title', 'N/A')}")
                    print(f"  描述长度: {len(pr_info.get('body', ''))} 字符")
                except Exception as e:
                    print(f"⚠️ 获取源PR信息失败: {e}")
                    print(f"⚠️ 创建PR时将使用默认标题和描述")

            # 10. 切换到目标分支，如果不存在则创建
            if not target_branch:
                print(f"❌ 目标分支未指定")
                return False
            # 获取默认分支作为目标分支的基分支
            result = self.run_git_command(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
            default_branch = "main"
            if result.returncode == 0:
                default_branch = result.stdout.strip().replace("origin/", "")

            # 切换到目标分支，如果不存在则创建
            if not self.checkout_or_create_branch(target_branch, create_new=True, based_on=default_branch):
                print(f"❌ 切换到目标分支 {target_branch} 失败")
                return False

            # 11. 创建cherry-pick分支
            if not source_branch_name:
                # 新分支名格式：cherry-pick-pr-{pr_num}-to-{target_branch}
                # 清理目标分支名中的非法字符
                if target_branch:
                    clean_target_branch = re.sub(r"[^\w\-/]", "-", target_branch)
                    # 将斜杠替换为短横线
                    clean_target_branch = clean_target_branch.replace("/", "-")
                    source_branch_name = f"cherry-pick-pr-{pr_num}-to-{clean_target_branch}"
                else:
                    source_branch_name = f"cherry-pick-pr-{pr_num}"

            print(f"🌿 创建cherry-pick分支: {source_branch_name}")

            # 12. 安全创建cherry-pick分支
            if not self.create_branch_safe(source_branch_name, target_branch):
                print(f"❌ 创建分支 {source_branch_name} 失败")
                return False

            # 13. 执行cherry-pick
            if not self.cherry_pick_commits(commit_shas):
                print("❌ cherry-pick执行失败")
                print(f"ℹ️ cherry-pick冲突，需要手动解决冲突。")
                print(f"   分支: {source_branch_name}")
                print(f"   提交: {commit_shas[0][:8]} 等")
                return False

            # 14. 推送更改
            if not self.push_changes(source_branch_name):
                print("❌ 推送更改失败")
                return False

            # 15. 自动创建PR（如果启用）
            if create_pr:
                if not self.create_pull_request(
                    self.target_repo, target_branch, source_branch_name, pr_info, title_prefix, body_tail
                ):
                    print("❌ PR创建失败")
                    return False
            else:
                if self.personal_repo:
                    print(f"ℹ️ 自动推送完成，分支已推送到个人仓库: {self.personal_repo}")
                    print(f"   分支: {source_branch_name}")
                    print(f"   如需创建PR，请使用: --create-pr 参数")
                else:
                    print(f"ℹ️ 自动推送完成，分支: {source_branch_name}")
                    print(f"   如需创建PR，请使用: --create-pr 参数")

        except Exception as e:
            print(f"❌ 执行过程中发生错误: {e}")
            return False

        finally:
            # 16. 清理可能包含token的远程仓库
            if self.using_existing_repo:
                self.remove_sensitive_remotes()

        print("\n" + "=" * 60)
        print("🎉 Cherry-pick流程完成!" + (" [DRY-RUN模式未执行实际操作]" if self.dry_run else ""))
        if self.using_existing_repo:
            print("ℹ️ 使用现有仓库，已清理可能包含token的远程仓库")
        print("=" * 60)
        return True


def main():
    """主函数，处理命令行参数"""
    parser = argparse.ArgumentParser(
        description="自动Cherry-pick机器人",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 不指定本地仓库路径，自动在临时目录克隆仓库
  %(prog)s https://github.com/owner/repo/pull/123 --target-branch main

  # 使用现有仓库进行cherry-pick
  %(prog)s https://github.com/owner/repo/pull/123 --target-branch main -r /path/to/existing/repo

  # 推送到个人仓库（fork）并自动创建PR
  %(prog)s https://github.com/owner/repo/pull/123 --target-branch main --personal-repo yourname/fork-repo --create-pr

  # 自动确认模式
  %(prog)s https://github.com/owner/repo/pull/123 --target-branch main --personal-repo yourname/fork-repo --create-pr -y

  # 自动创建PR
  %(prog)s https://github.com/owner/repo/pull/123 --target-branch main --create-pr

  # 指定源分支名
  %(prog)s https://github.com/owner/repo/pull/123 --target-branch main -s feature/cherry-pick-123

  # 指定源分支名（长格式）
  %(prog)s https://github.com/owner/repo/pull/123 --target-branch main --source-branch-name feature/cherry-pick-123

  # 自定义标题前缀和描述尾部
  %(prog)s https://github.com/owner/repo/pull/123 --target-branch main --create-pr --title-prefix "Backport:" --body-tail "This PR was created automatically. original PR: {pr_url}."

  # Dry-run模式
  %(prog)s https://github.com/owner/repo/pull/123 --target-branch main --dry-run

  # 使用token
  %(prog)s https://github.com/owner/repo/pull/123 --target-branch main -t your_token

  # 指定源分支名
  %(prog)s https://github.com/owner/repo/pull/123 --target-branch main --source-branch-name feature/cherry-pick-123
        """,
    )

    # 必需参数
    parser.add_argument("pr_url", help="PR链接地址 (GitHub或Gitee)")

    # 可选参数
    parser.add_argument("-r", "--repo-path", help="本地Git仓库路径 (不指定则在临时目录中克隆)")
    parser.add_argument("-t", "--token", help="API访问令牌 (Github Token或Gitee Token)，也用于Git操作认证")
    parser.add_argument("-y", "--yes", action="store_true", help="自动确认所有提示，无需手动输入")
    parser.add_argument(
        "-s", "--source-branch-name", help="源分支名称 (默认: 自动生成，格式: cherry-pick-pr-<pr号>-to-<目标分支>)"
    )
    parser.add_argument("--target-repo", help="目标仓库 (格式: owner/repo, 默认: 与源PR相同)")
    parser.add_argument("--target-branch", help="目标分支名称")
    parser.add_argument("--personal-repo", help="个人仓库 (fork仓库) (格式: owner/repo, 用于推送分支和创建PR)")
    parser.add_argument("--create-pr", action="store_true", help="自动创建PR")
    parser.add_argument("--title-prefix", help="PR标题前缀 (默认: 'Cherry-pick:')")
    parser.add_argument("--body-tail", help="PR描述尾部，将追加到PR描述末尾")
    parser.add_argument("--dry-run", action="store_true", help="模拟运行，不执行实际操作")
    parser.add_argument("--token-env-var", help="从环境变量读取token的变量名 (如: GITHUB_TOKEN)")
    parser.add_argument("--patch", help="生成format-patch文件，可以是单个文件或目录")

    args = parser.parse_args()

    # 处理token
    token = args.token
    if not token and args.token_env_var:
        token = os.getenv(args.token_env_var)
        if token:
            print(f"✅ 从环境变量 {args.token_env_var} 获取token")

    # 创建机器人实例并运行
    bot = CherryPickBot(token=token, dry_run=args.dry_run, auto_confirm=args.yes)

    success = bot.run(
        pr_url=args.pr_url,
        target_branch=args.target_branch,
        repo_path=args.repo_path,
        target_repo=args.target_repo,
        personal_repo=args.personal_repo,
        create_pr=args.create_pr,
        source_branch_name=args.source_branch_name,
        token=token,
        title_prefix=args.title_prefix,
        body_tail=args.body_tail,
        patch_file=args.patch,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
