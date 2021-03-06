# Copyright 2018 Datawire. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
from subprocess import Popen, PIPE, DEVNULL, CalledProcessError
from threading import Thread
from time import time, sleep
from typing import List, Optional

from inspect import getframeinfo, currentframe
import os

from telepresence.cache import Cache
from telepresence.output import Output
from telepresence.span import Span
from telepresence.utilities import str_command


class Runner(object):
    """Context for running subprocesses."""

    def __init__(
        self, output: Output, kubectl_cmd: str, verbose: bool
    ) -> None:
        """
        :param output: The Output instance for the session
        :param kubectl_cmd: Command to run for kubectl, either "kubectl" or
            "oc" (for OpenShift Origin).
        :param verbose: Whether subcommand should run in verbose mode.
        """
        self.output = output
        self.kubectl_cmd = kubectl_cmd
        self.verbose = verbose
        self.start_time = time()
        Optional  # Avoid Pyflakes F401
        self.current_span = None  # type: Optional[Span]
        self.counter = 0

        # Log some version info
        report = (
            ["kubectl", "version", "--short"],
            ["oc", "version"],
            ["uname", "-a"],
        )
        for command in report:
            try:
                self.popen(command)
            except OSError:
                pass
        self.output.write("Python {}".format(sys.version))

        cache_dir = os.path.expanduser("~/.cache/telepresence")
        os.makedirs(cache_dir, exist_ok=True)
        self.cache = Cache.load(os.path.join(cache_dir, "cache.json"))
        self.cache.invalidate(12 * 60 * 60)

    @classmethod
    def open(cls, logfile_path, kubectl_cmd: str, verbose: bool):
        """
        :return: File-like object for the given logfile path.
        """
        output = Output(logfile_path)
        return cls(output, kubectl_cmd, verbose)

    def span(self, name: str = "", context=True, verbose=True) -> Span:
        """Write caller's frame info to the log."""

        if context:
            frame = currentframe()
            assert frame is not None  # mypy
            info = getframeinfo(frame.f_back)
            tag = "{}:{}({})".format(
                os.path.basename(info.filename), info.lineno,
                "{},{}".format(info.function, name) if name else info.function
            )
        else:
            tag = name
        s = Span(self, tag, self.current_span, verbose=verbose)
        self.current_span = s
        s.begin()
        return s

    def write(self, message: str, prefix="TEL") -> None:
        """Don't use this..."""
        return self.output.write(message, prefix)

    def read_logs(self) -> str:
        """Return the end of the contents of the log"""
        sleep(2.0)
        return self.output.read_logs()

    def set_success(self, flag: bool) -> None:
        """Indicate whether the command succeeded"""
        Span.emit_summary = flag
        self.output.write("Success. Starting cleanup.")

    def command_span(self, track, args):
        return self.span(
            "{} {}".format(track, str_command(args))[:80],
            False,
            verbose=False
        )

    def make_logger(self, track, capture=None):
        """Create a logger that optionally captures what is logged"""
        prefix = "{:>3d}".format(track)

        if capture is None:

            def logger(line):
                """Just log"""
                if line is not None:
                    self.output.write(line, prefix=prefix)
        else:

            def logger(line):
                """Log and capture"""
                capture.append(line)
                if line is not None:
                    self.output.write(line, prefix=prefix)

        return logger

    def launch_command(self, track, out_cb, err_cb, args, **kwargs) -> Popen:
        """Call a command, generate stamped, logged output."""
        try:
            process = launch_command(args, out_cb, err_cb, **kwargs)
        except OSError as exc:
            self.output.write("[{}] {}".format(track, exc))
            raise
        # Grep-able log: self.output.write("CMD: {}".format(str_command(args)))
        return process

    def run_command(self, track, msg1, msg2, out_cb, err_cb, args, **kwargs):
        """Run a command synchronously"""
        self.output.write("[{}] {}: {}".format(track, msg1, str_command(args)))
        span = self.command_span(track, args)
        process = self.launch_command(track, out_cb, err_cb, args, **kwargs)
        process.wait()
        spent = span.end()
        retcode = process.poll()
        if retcode:
            self.output.write(
                "[{}] exit {} in {:0.2f} secs.".format(track, retcode, spent)
            )
            raise CalledProcessError(retcode, args)
        if spent > 1:
            self.output.write(
                "[{}] {} in {:0.2f} secs.".format(track, msg2, spent)
            )

    def check_call(self, args, **kwargs):
        """Run a subprocess, make sure it exited with 0."""
        self.counter = track = self.counter + 1
        out_cb = err_cb = self.make_logger(track)
        self.run_command(
            track, "Running", "ran", out_cb, err_cb, args, **kwargs
        )

    def get_output(self, args, reveal=False, **kwargs) -> str:
        """Return (stripped) command result as unicode string."""
        self.counter = track = self.counter + 1
        capture = []  # type: List[str]
        if reveal or self.verbose:
            out_cb = self.make_logger(track, capture=capture)
        else:
            out_cb = capture.append
        err_cb = self.make_logger(track)
        cpe_exc = None
        try:
            self.run_command(
                track, "Capturing", "captured", out_cb, err_cb, args, **kwargs
            )
        except CalledProcessError as exc:
            cpe_exc = exc
        # Wait for end of stream to be recorded
        while not capture or capture[-1] is not None:
            sleep(0.1)
        del capture[-1]
        output = "".join(capture).strip()
        if cpe_exc:
            raise CalledProcessError(cpe_exc.returncode, cpe_exc.cmd, output)
        return output

    def popen(self, args, **kwargs) -> Popen:
        """Return Popen object."""
        self.counter = track = self.counter + 1
        out_cb = err_cb = self.make_logger(track)

        def done(proc):
            self._popen_done(track, proc)

        self.output.write(
            "[{}] Launching: {}".format(track, str_command(args))
        )
        process = self.launch_command(
            track, out_cb, err_cb, args, done=done, **kwargs
        )
        return process

    def _popen_done(self, track, process):
        retcode = process.poll()
        if retcode is not None:
            self.output.write("[{}] exit {}".format(track, retcode))

    def kubectl(self, context: str, namespace: str,
                args: List[str]) -> List[str]:
        """Return command-line for running kubectl."""
        result = [self.kubectl_cmd]
        if self.verbose:
            result.append("--v=4")
        result.extend(["--context", context])
        result.extend(["--namespace", namespace])
        result += args
        return result

    def get_kubectl(
        self, context: str, namespace: str, args: List[str], stderr=None
    ) -> str:
        """Return output of running kubectl."""
        return self.get_output(
            self.kubectl(context, namespace, args), stderr=stderr
        )

    def check_kubectl(
        self, context: str, namespace: str, kubectl_args: List[str], **kwargs
    ) -> None:
        """Check exit code of running kubectl."""
        self.check_call(
            self.kubectl(context, namespace, kubectl_args), **kwargs
        )


def launch_command(args, out_cb, err_cb, done=None, **kwargs):
    """
    Launch subprocess with args, kwargs.
    Log stdout and stderr by calling respective callbacks.
    """

    def pump_stream(callback, stream):
        """Pump the stream"""
        for line in stream:
            callback(line)
        callback(None)

    def joiner():
        """Wait for streams to finish, then call done callback"""
        for th in threads:
            th.join()
        done(process)

    kwargs = kwargs.copy()
    in_data = kwargs.get("input")
    if "input" in kwargs:
        del kwargs["input"]
        assert kwargs.get("stdin") is None, kwargs["stdin"]
        kwargs["stdin"] = PIPE
    elif "stdin" not in kwargs:
        kwargs["stdin"] = DEVNULL
    kwargs.setdefault("stdout", PIPE)
    kwargs.setdefault("stderr", PIPE)
    kwargs["universal_newlines"] = True  # Text streams, not byte streams
    process = Popen(args, **kwargs)
    threads = []
    if process.stdout:
        thread = Thread(
            target=pump_stream, args=(out_cb, process.stdout), daemon=True
        )
        thread.start()
        threads.append(thread)
    if process.stderr:
        thread = Thread(
            target=pump_stream, args=(err_cb, process.stderr), daemon=True
        )
        thread.start()
        threads.append(thread)
    if done and threads:
        Thread(target=joiner, daemon=True).start()
    if in_data:
        process.stdin.write(str(in_data, "utf-8"))
        process.stdin.close()
    return process
