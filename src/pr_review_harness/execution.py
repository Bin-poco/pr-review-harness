"""Explicit test execution policy and bounded process supervision."""

import json
import os
import re
import selectors
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class ExecutionPolicy:
    # Python callers keep the historical trusted-local behavior. Public review CLI
    # defaults to docker; remote CLI refuses local execution altogether.
    backend: str = "local"
    image: str = "python:3.12-slim"
    image_id: str | None = None
    platform: str | None = None
    uid: int = field(default_factory=lambda: os.getuid() or 65534)
    gid: int = field(default_factory=lambda: os.getgid() if os.getuid() else 65534)
    timeout: int = 20
    memory_mb: int = 256
    cpus: float = 1.0
    pids: int = 64
    output_bytes: int = 8000

    def __post_init__(self):
        if self.backend not in {"local", "docker"}:
            raise ValueError("Test backend must be local or docker")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,255}", self.image):
            raise ValueError("Invalid test image reference")
        if self.image_id and not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_id):
            raise ValueError("Invalid pinned test image ID")
        for value, low, high in (
            (self.timeout, 1, 120),
            (self.memory_mb, 64, 2048),
            (self.pids, 16, 256),
            (self.output_bytes, 1000, 32000),
            (self.uid, 1, 2**31 - 1),
            (self.gid, 0, 2**31 - 1),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Test limits are outside the supported range")
        if not 0.1 <= self.cpus <= 4:
            raise ValueError("Test CPUs must be between 0.1 and 4")

    def prepare(self):
        """Fail before model calls; never fall back to host execution or pull images."""
        if self.backend == "local":
            return self
        if os.getuid() and (self.uid, self.gid) != (os.getuid(), os.getgid()):
            raise ValueError("Test UID/GID differ from the owner of the private snapshot export")
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", self.image_id or self.image],
                env=docker_environment(),
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(
                "Docker is unavailable; start Docker and preload the test image"
            ) from exc
        if result.returncode:
            raise ValueError("Test image is unavailable; preload it with docker pull before review")
        try:
            value = json.loads(result.stdout)[0]
            resolved = replace(
                self, image_id=value["Id"], platform=f"{value['Os']}/{value['Architecture']}"
            )
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            raise ValueError("Docker returned an invalid image identity") from exc
        if resolved.platform != "linux/amd64" and resolved.platform != "linux/arm64":
            raise ValueError("Tests require a Linux amd64/arm64 container image")
        if self.platform and self.platform != resolved.platform:
            raise ValueError("Pinned test image platform changed")
        if self.image_id and self.image_id != resolved.image_id:
            raise ValueError("Pinned test image identity changed")
        return resolved

    def manifest(self, enabled=True):
        return {"enabled": enabled, **asdict(self)} if enabled else {"enabled": False}

    @classmethod
    def from_manifest(cls, value):
        return cls(**{k: v for k, v in (value or {}).items() if k != "enabled"})


def docker_environment():
    # Docker Desktop needs HOME to find its context. This is the CLI environment,
    # never the container environment; model/GitHub secrets are deliberately absent.
    return {
        k: os.environ[k]
        for k in (
            "PATH",
            "HOME",
            "LANG",
            "SYSTEMROOT",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        )
        if k in os.environ
    }


def bounded_process(command, *, cwd: Path, environment, timeout, output_bytes):
    """Drain pipes continuously, retaining only a tail and enforcing a deadline.

    Unlike a temporary logfile, noisy tests cannot fill the host disk. Callers
    must clean up any external execution resource (a container) in their finally.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    tail = bytearray()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                ready = selector.select(min(remaining, 0.1))
                if ready:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    tail.extend(chunk)
                    del tail[:-output_bytes]
                    if not chunk:
                        process.wait(timeout=max(0.01, remaining))
                        break
                elif process.poll() is not None:
                    break
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        process.stdout.close()
    return (None if timed_out else process.returncode), tail.decode("utf-8", errors="replace")
