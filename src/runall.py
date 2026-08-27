import os
from src.config import get_test_command
import src.config as config
from src.utils import subprocess_run, Timer, run_test_files_parallel
import time


class Runall:
    def __init__(self, project_path: str, conda_env_name: str, selected_tests: set, n: int = 60, use_isolation: bool = False):
        self.project_path = project_path
        self.conda_env_name = conda_env_name
        self.n = n
        self.test_time = 0.0
        self.selected_tests = selected_tests

    def get_test_time(self):
        return self.test_time

    def get_tests_to_run(self, init: bool = False):
        return self.selected_tests

    def run_and_update(self, tests_to_run):
        with Timer("Run tests and update deps") as _:
            if len(tests_to_run) == 0:
                return
            tests_to_run_file = "tests_to_run.txt"
            tests_to_run_path = os.path.join(self.project_path, tests_to_run_file)

            with open(tests_to_run_path, "w") as f:
                f.write("\n".join(tests_to_run))
            test_command = get_test_command(self.project_path)

            start_time = time.time()
            run_test_files_parallel(
                self.project_path,
                self.conda_env_name,
                test_command,
                tests_to_run,
                self.n,
                timeout=config.PER_FILE_TEST_TIMEOUT,
            )
            end_time = time.time()
            self.test_time = end_time - start_time

            try:
                os.remove(tests_to_run_path)
            except:
                pass
