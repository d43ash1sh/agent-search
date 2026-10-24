from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from services.common.config import settings

logger = logging.getLogger("agent-search.docker")


def is_port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    """Check if a network port is accepting TCP connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def is_docker_available() -> bool:
    """Check if docker and docker compose CLI binaries are in PATH."""
    return shutil.which("docker") is not None


def is_docker_daemon_running() -> bool:
    """Verify if the Docker daemon is responding to CLI commands."""
    if not is_docker_available():
        return False
    try:
        res = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
            check=False,
        )
        return res.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def get_compose_file() -> Path | None:
    """Locate docker-compose.yml in project root."""
    root_compose = Path(__file__).resolve().parent.parent.parent / "docker-compose.yml"
    if root_compose.exists():
        return root_compose
    local_compose = Path("docker-compose.yml")
    return local_compose if local_compose.exists() else None


def start_docker_containers(services: list[str] | None = None) -> bool:
    """Run docker compose up -d for specified services (or all if omitted)."""
    compose_file = get_compose_file()
    if not compose_file:
        logger.warning("docker-compose.yml not found. Skipping docker compose up.")
        return False

    cmd = ["docker", "compose", "-f", str(compose_file), "up", "-d"]
    if services:
        cmd.extend(services)

    logger.info("Executing: %s", " ".join(cmd))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60.0, check=False)
        if res.returncode == 0:
            logger.info("Docker compose started successfully: %s", (res.stdout or "").strip())
            return True
        else:
            logger.warning("Docker compose failed (code %d): %s", res.returncode, (res.stderr or "").strip())
            return False
    except Exception as exc:
        logger.warning("Error running docker compose: %s", exc)
        return False


def stop_docker_containers() -> bool:
    """Run docker compose down to stop running containers."""
    compose_file = get_compose_file()
    if not compose_file:
        return False

    cmd = ["docker", "compose", "-f", str(compose_file), "down"]
    logger.info("Stopping Docker containers: %s", " ".join(cmd))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30.0, check=False)
        return res.returncode == 0
    except Exception as exc:
        logger.warning("Error stopping docker containers: %s", exc)
        return False


def ensure_infrastructure_online(
    auto_start_docker: bool = True,
    wait_timeout: float = 20.0,
) -> dict[str, Any]:
    """Ensure PostgreSQL and Redis infrastructure are accessible.

    1. Checks if Postgres (port 5432) and Redis (port 6379) are already reachable (e.g. running in WSL or native).
    2. If not reachable and auto_start_docker is True, attempts to start them via docker compose.
    3. Waits for services to become healthy before returning.
    """
    from urllib.parse import urlsplit

    pg_host = urlsplit(settings.postgres_url).hostname or "127.0.0.1"
    pg_port = urlsplit(settings.postgres_url).port or 5432

    redis_host = urlsplit(settings.redis_url).hostname or "127.0.0.1"
    redis_port = urlsplit(settings.redis_url).port or 6379

    pg_alive = is_port_in_use(pg_host, pg_port)
    redis_alive = is_port_in_use(redis_host, redis_port)

    started_by_docker = False

    if pg_alive and redis_alive:
        logger.info(
            "Infrastructure online: PostgreSQL (%s:%d) and Redis (%s:%d) are active.",
            pg_host,
            pg_port,
            redis_host,
            redis_port,
        )
        return {
            "postgres": True,
            "redis": True,
            "started_by_docker": False,
            "status": "ready",
        }

    if not auto_start_docker:
        logger.warning(
            "Infrastructure offline (PG: %s, Redis: %s). Auto-docker disabled.",
            "online" if pg_alive else "offline",
            "online" if redis_alive else "offline",
        )
        return {
            "postgres": pg_alive,
            "redis": redis_alive,
            "started_by_docker": False,
            "status": "degraded",
        }

    # Attempt to start via Docker Compose
    if not is_docker_daemon_running():
        logger.warning("Docker daemon is not running. Using fallback in-memory broker / SQLite if needed.")
        return {
            "postgres": pg_alive,
            "redis": redis_alive,
            "started_by_docker": False,
            "status": "docker_daemon_unavailable",
        }

    needed_services: list[str] = []
    if not pg_alive:
        needed_services.append("postgres")
    if not redis_alive:
        needed_services.append("redis")

    logger.info("Auto-starting missing infrastructure via Docker Compose: %s", needed_services)
    docker_ok = start_docker_containers(needed_services)
    if not docker_ok:
        return {
            "postgres": pg_alive,
            "redis": redis_alive,
            "started_by_docker": False,
            "status": "docker_compose_failed",
        }

    started_by_docker = True

    # Wait for ports to become open
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        pg_alive = is_port_in_use(pg_host, pg_port)
        redis_alive = is_port_in_use(redis_host, redis_port)
        if pg_alive and redis_alive:
            logger.info("PostgreSQL and Redis are now responsive.")
            break
        time.sleep(1.0)

    return {
        "postgres": pg_alive,
        "redis": redis_alive,
        "started_by_docker": started_by_docker,
        "status": "ready" if (pg_alive and redis_alive) else "timeout",
    }
