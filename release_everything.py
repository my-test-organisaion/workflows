import os
from pathlib import Path
import re
import sys
import time
import requests
from contextlib import contextmanager

import git
from github import Github, GitRelease
import shutil

from release import run, timeit
from supervisely.cli.release import get_app_from_instance, get_appKey


SUPERVISELY_ECOSYSTEM_REPOSITORY_V2_URL = "https://raw.githubusercontent.com/supervisely-ecosystem/repository/master/README_v2.md"


@contextmanager
def cd(newdir):
    prevdir = os.getcwd()
    os.chdir(os.path.expanduser(newdir))
    try:
        yield
    finally:
        os.chdir(prevdir)


def insert_user_and_pass_to_url(url, username, password):
    username_password = f"{username}:{password}@" if username and password else ""
    return "".join(["https://", username_password, *url.split("https://")[1:]])


def parse_ecosystem_repository_page(url):
    r = requests.get(url)
    content = r.content.decode("utf-8")
    results = [
        "https://github.com/" + res
        for res in re.findall(r"https://github.com/(.*)\n", content)
    ]
    return results


def get_slug(repo_url):
    return "/".join(repo_url.split("/")[1:])


def remove_prefix(input_string, prefix):
    if prefix and input_string.startswith(prefix):
        return input_string[len(prefix) :]
    return input_string


def get_subapp_path(app_url: str):
    app_url = remove_prefix(remove_prefix(app_url, "https://"), "http://")
    # app_url = app_url.removeprefix("https://").removeprefix("http://")
    subapp_path = "/".join(app_url.split("/")[3:])
    return subapp_path if subapp_path else "__ROOT_APP__"


def get_repo_url(app_url: str):
    app_url = remove_prefix(remove_prefix(app_url, "https://"), "http://")
    # app_url = app_url.removeprefix("https://").removeprefix("http://")
    return "/".join(app_url.split("/")[:3])


def is_release_tag(tag_name):
    return re.fullmatch("v\d+\.\d+\.\d+", tag_name) != None


def sorted_releases(releases):
    key = lambda release: [int(x) for x in release.tag_name[1:].split(".")]
    return sorted(releases, key=key)


@timeit
def clone_repo(url):
    repo_dir = os.path.join(os.getcwd(), "repo")
    username = os.environ.get("GIT_USERNAME")
    password = os.environ.get("GIT_PASSWORD")
    return git.Repo.clone_from(
        insert_user_and_pass_to_url(url, username, password), repo_dir
    )


def delete_repo(attempt=0):
    repo_dir = Path(os.getcwd()).joinpath("repo")
    if repo_dir.exists():
        try:
            shutil.rmtree(repo_dir)
        except:
            if attempt >= 3:
                raise
            time.sleep(1)
            delete_repo(attempt=attempt + 1)


def get_instance_releases(server_address, api_token, repo, subapp_path, repo_url):
    if subapp_path == "__ROOT_APP__":
        subapp_path = None
    appKey = get_appKey(repo, subapp_path, repo_url)
    try:
        app_info = get_app_from_instance(appKey, api_token, server_address)
    except PermissionError:
        return None
    if app_info is None:
        return None

    versions = [release["version"] for release in app_info["meta"]["releases"]]
    return versions


def is_published(release: GitRelease.GitRelease):
    return not (release.prerelease or release.draft)


def release_app(app_url, add_slug, release_branches):
    app_url = (
        app_url.replace(".www", "")
        .replace("/tree/master", "")
        .replace("/tree/main", "")
    )
    # app_url = "https://github.com/org/repo/path/to/subapp"
    repo_url = get_repo_url(app_url)
    subapp_path = get_subapp_path(app_url)
    slug = get_slug(repo_url)
    api_token = os.getenv("API_TOKEN", None)
    server_address = os.getenv("SERVER_ADDRESS", None)
    github_access_token = os.getenv("GITHUB_ACCESS_TOKEN", None)

    delete_repo()
    print()
    print("Cloning repo...")
    repo = clone_repo("https://github.com/" + slug)
    print("Done cloning repo\n")

    instance_releases = get_instance_releases(
        server_address=server_address,
        api_token=api_token,
        repo=repo,
        subapp_path=subapp_path,
        repo_url=f"https://github.com/{slug}",
    )
    if instance_releases is None:
        print()
        print("Can't receive instance releases. Will try to release all")
        instance_releases = []

    GH = Github(github_access_token)
    repo_name = remove_prefix(repo_url, "github.com/")
    # repo_name = repo_url.removeprefix("github.com/")
    gh_repo = GH.get_repo(repo_name)
    gh_releases = sorted_releases(
        [
            rel
            for rel in gh_repo.get_releases()
            if is_release_tag(rel.tag_name) and is_published(rel)
        ]
    )
    print()
    print("App url:", app_url)
    print("Slug:", slug)
    print("Subapp path:", subapp_path)
    print("GH releases:", [release.tag_name for release in gh_releases])
    print("Instance releases:", instance_releases)

    success = True
    with cd(Path(os.getcwd()).joinpath("repo")):
        if len(gh_releases) == 0 and release_branches:
            # if no releases, then release master branch
            print()
            print("No GitHub releases found. Will release master/main branch")
            branch = None
            for br in repo.branches:
                if br.name in ["master", "main"]:
                    br.checkout()
                    branch = br
                    break
            if branch is not None:
                print("Releasing master/main branch")
                print()
                success = (
                    run(
                        slug=slug,
                        subapp_paths=[subapp_path],
                        server_address=server_address,
                        api_token=api_token,
                        github_access_token=github_access_token,
                        release_version=branch.name,
                        release_title=branch.name,
                        ignore_sly_releases=True,
                        add_slug=add_slug,
                        check_previous_releases=False,
                    )
                    and success
                )
            else:
                print("No master/main branch found. Nothing released")
                print()
                repo.git.clear_cache()
                return False

        if set(instance_releases) == set([release.tag_name for release in gh_releases]):
            print()
            print("All releases are released for this App")
            print()
            repo.git.clear_cache()
            return True

        for gh_release in [
            gh_release
            for gh_release in gh_releases
            if gh_release.tag_name not in instance_releases
        ]:
            gh_release: GitRelease.GitRelease
            release_version = gh_release.tag_name
            release_name = gh_release.title
            if release_name is None or release_name == "":
                release_name = " "
            repo.git.checkout(release_version)
            success = (
                run(
                    slug=slug,
                    subapp_paths=[subapp_path],
                    server_address=server_address,
                    api_token=api_token,
                    github_access_token=github_access_token,
                    release_version=release_version,
                    release_title=release_name,
                    ignore_sly_releases=True,
                    add_slug=add_slug,
                    check_previous_releases=False,
                )
                and success
            )

    repo.git.clear_cache()
    return success


if __name__ == "__main__":
    """
    Usage:
    python release_everything.py [add_slug = 1/0] [(optional)apps_repository_gh_url]
    default apps_repository_gh_url = https://raw.githubusercontent.com/supervisely-ecosystem/repository/master/README_v2.md
    """
    try:
        slug = int(sys.argv[1])
    except IndexError:
        slug = 1
    try:
        apps_repository_gh_url = sys.argv[2]
    except IndexError:
        apps_repository_gh_url = SUPERVISELY_ECOSYSTEM_REPOSITORY_V2_URL
    try:
        release_branches = int(sys.argv[3])
    except IndexError:
        release_branches = 0
    app_urls = parse_ecosystem_repository_page(apps_repository_gh_url)
    try:
        with open("logs/progress.txt", "r") as f:
            released_urls = [line[:-1] for line in f.readlines()]
    except FileNotFoundError:
        released_urls = []
        open("logs/progress.txt", "x").close()
    for app_url in app_urls:
        if app_url in released_urls:
            print("App already released:", app_url)
            continue
        success = release_app(
            app_url, add_slug=slug == 1, release_branches=release_branches == 1
        )
        if success:
            with open("logs/progress.txt", "a") as f:
                f.write(app_url + "\n")
