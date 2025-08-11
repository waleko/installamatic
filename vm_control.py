import argparse
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from difflib import get_close_matches
from io import TextIOWrapper
from typing import List, Optional
import shutil
import tempfile

from install_test.consts import BUILD_LOGS, FASTAPI
from install_test.utils import notify
from git_scraping import get_repository_language


SETUP_FILE = "resources/setup.sh"
IMAGE_NAME = "temp_image"
TIMEOUT = 60 * 20


class OutOfStorage(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)


class VMController:
    def __init__(self, logs: Optional[str] = "STDOUT") -> None:
        self.logs = logs
        if self.logs is not None:
            with open(self.logs, "w") as f:
                f.write("")

    def log(self, msg, flag="a"):
        if self.logs == "STDOUT":
            print(msg)
        elif self.logs is not None:
            with open(self.logs, flag) as f:
                f.write(msg + "\n")

    def get_dockerfile(self, target_repo: str) -> str:
        """returns path to a preset dockerfile based on the langauge of target repo."""
        language = get_repository_language(target_repo).lower()
        dockerfile = os.path.abspath(
            f"resources/default_dockerfiles/{language}/Dockerfile"
        )
        if os.path.exists(dockerfile):
            return dockerfile
        else:
            raise ValueError(f"No dockerfile found for langauge: {language}")

    def open_machine(self):
        """No-op on local mode. Ensure Docker is available locally."""
        try:
            subprocess.run(["docker", "version"], check=True, capture_output=True)
            self.log("Docker detected locally.")
        except Exception:
            raise RuntimeError(
                "Docker does not seem to be available locally. Please install and start Docker."
            )

    def setup_repo(self, target_repo: str, dockerfile: str, ref: Optional[str] = None):
        """
        Clone target repo into a local temporary directory,
        then copy the dockerfile into the repo as Dockerfile.
        """
        # make temp directory
        tmp_dir = tempfile.mkdtemp(prefix="repo_build_")
        self.log(f"TEMP DIR: {tmp_dir}")
        # clone target repo in temp directory
        repo_name = target_repo.split("/")[-1][:-4]
        try:
            resp = subprocess.run(
                ["git", "clone", "--recursive", target_repo],
                cwd=tmp_dir,
                capture_output=True,
                timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            resp = subprocess.run(
                ["git", "clone", "--recursive", target_repo],
                cwd=tmp_dir,
                capture_output=True,
                timeout=TIMEOUT,
            )

        self.log(resp.stderr.decode("utf-8").strip())
        self.log(resp.stdout.decode("utf-8").strip())

        # get name of the directory where the repo was cloned to (-4 to remove '.git')
        repo_dir = os.path.join(tmp_dir, repo_name)
        print(repo_dir)

        # optionally checkout specific ref
        if ref is not None:
            subprocess.run(["git", "checkout", ref], cwd=repo_dir, capture_output=True)

        # remove .dockerignore if it exists
        dockerignore_path = os.path.join(repo_dir, ".dockerignore")
        if os.path.exists(dockerignore_path):
            try:
                os.remove(dockerignore_path)
            except Exception:
                pass

        # copy dockerfile into repo
        shutil.copyfile(dockerfile, os.path.join(repo_dir, "Dockerfile"))
        return tmp_dir, repo_dir

    def build_project(self, repo_dir: str, logs: str) -> bool:
        """Run docker build locally and stream progress."""
        # build dockerfile locally
        cmd = [
            "bash",
            "-lc",
            f"cd {repo_dir} ; docker build --no-cache -t {IMAGE_NAME} .",
        ]
        with open(logs, "a") as f:
            progress, timeout = self.monitor_process(cmd, f, TIMEOUT)
        if timeout:
            with open(logs, "a") as f:
                progress, timeout = self.monitor_process(cmd, f, TIMEOUT)

        #     progress = subprocess.Popen(cmd, stdout=f, stderr=f)
        # progress.wait()

        with open(logs, "r") as f:
            output = f.readlines()
        passed = False
        ran = False
        for i, line in enumerate(output):
            if "fatal" in line and "No space left on device" in line:
                raise OutOfStorage()
            if (
                (
                    ("==" in line and " in " in line)
                    or "snapshots" in line
                    or ("tests" in line)
                    or len(output) - i < 30
                )
                and "passed" in line
            ) or (
                ran
                and (
                    len(line.split()) > 0
                    and line.split()[-1].strip() == "OK"
                    or ("(" in line and line.split("(")[0].split()[-1] == "OK")
                )
            ):
                passed = True
            elif "Ran" in line and "tests in" in line:
                ran = True
        if timeout:
            msg = (
                "process timed out twice! "
                f"(took more than {TIMEOUT} seconds) Aborting...\n"
            )
            f.write(msg)
            notify(msg)
            return False
        if not passed:
            try:
                err = "Error running docker build locally."
                self.log(err)
                print(err)
            except:
                pass
            return False
        else:
            succ = (
                "At least 1 test passed.\n"
                "Docker build completed successfully locally."
            )
            self.log(succ)
            print(succ)
            return True

    def monitor_process(self, cmd: List[str], f: TextIOWrapper, timeout_val: int):
        progress = subprocess.Popen(cmd, stdout=f, stderr=f)
        start_time = time.time()
        timeout = False
        interrupted = False
        try:
            while True:
                # get latest output
                if progress.poll() is not None:
                    break
                elif time.time() - start_time > timeout_val:
                    if not interrupted:
                        # First try to cancel the process with an interrupt
                        # os.killpg(os.getpgid(progress.pid), signal.SIGINT)
                        os.kill(progress.pid, signal.SIGINT)
                        notify(f"INTERRUPTING")
                        start_time = time.time()
                        timeout_val = 20
                        interrupted = True
                    else:
                        # If the interrupt did not work, raise a timeout
                        raise subprocess.TimeoutExpired(cmd, timeout_val)

        except subprocess.TimeoutExpired:
            notify("KILLING PROCESS")
            progress.kill()
            timeout = True
        finally:
            progress.wait()
        return progress, timeout

    def clear_cache(self):
        subprocess.run(["docker", "system", "prune", "-a", "-f"])

    def cleanup(self, tmp_dir: str, keep_image: bool = False, keep_repo: bool = False):
        """Delete docker image and temporary file after execution."""
        if not keep_image:
            # remove newly created docker image if it exists
            try:
                inspect = subprocess.run(
                    ["docker", "image", "inspect", IMAGE_NAME],
                    capture_output=True,
                )
                if inspect.returncode == 0:
                    self.log("removing docker image...")
                    subprocess.run(
                        ["docker", "image", "rm", "-f", IMAGE_NAME],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            except Exception:
                pass
        if not keep_repo:
            # clear temp directory
            self.log("clearing temp directory")
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

    def test_dockerfile(
        self,
        target_repo: str,
        dockerfile: Optional[str] = None,
        keep_image: bool = False,
        keep_repo: bool = False,
        logs: Optional[str] = None,
        ref: Optional[str] = None,
    ):
        """
        Test a dockerfile by cloning the repo locally, copying the Dockerfile,
        and building the Docker image locally.
        """

        if dockerfile is None:
            dockerfile = self.get_dockerfile(target_repo)
            self.log(f"using dockerfile: {dockerfile}")

        with open(dockerfile, "r") as f:
            df_contents = f.read()
        self.log(df_contents, "w")

        self.open_machine()

        try:
            tmp_dir, repo_dir = self.setup_repo(target_repo, dockerfile, ref=ref)
            self.log("setup repo.")
            success = self.build_project(repo_dir=repo_dir, logs=logs or self.logs)
        except OutOfStorage:
            self.cleanup(tmp_dir)
            self.clear_cache()
            notify("RAN OUT OF STORAGE!! RESTARTING")
            tmp_dir, repo_dir = self.setup_repo(target_repo, dockerfile, ref=ref)
            self.log("setup repo.")
            success = self.build_project(repo_dir=repo_dir, logs=logs or self.logs)

        except Exception as e:
            success = False
            if __name__ == "__main__":
                raise e
            else:
                print(e)
        except KeyboardInterrupt as e:
            success = False
        self.cleanup(tmp_dir, keep_image=keep_image, keep_repo=keep_repo)

        return success


def test_dockerfile(
    url: str,
    dockerfile: str,
    repo_name: Optional[str] = None,
    vmc: Optional[VMController] = None,
    ref: Optional[str] = None,
) -> bool:
    os.makedirs("logs/dockerfiles", exist_ok=True)
    name = url.split("/")[-1][:-4]
    dockerfile_path = os.path.join("logs", "dockerfiles", "Dockerfile")

    with open(dockerfile_path, "w") as f:
        f.write(dockerfile)
    print(dockerfile)

    if vmc is None:
        os.makedirs(BUILD_LOGS, exist_ok=True)
        logs = f"{BUILD_LOGS}/{repo_name or name}.log"
        vmc = VMController(logs)

    (f"\nattempting to build using dockerfile, logs written to {vmc.logs}.")
    return vmc.test_dockerfile(url, dockerfile_path, ref=ref)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dockerfile",
        help="path to a dockerfile you want to test",
        default="resources/fastapi.dockerfile",
    )
    parser.add_argument(
        "--repo", help="url to a repo you want to test", default=FASTAPI
    )
    args = parser.parse_args()
    controller = VMController()
    controller.test_dockerfile(target_repo=args.repo, dockerfile=args.dockerfile)
