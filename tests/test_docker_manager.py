from __future__ import annotations

from services.common.docker_manager import (
    ensure_infrastructure_online,
    get_compose_file,
    is_docker_available,
    is_port_in_use,
)


def test_is_port_in_use_detects_closed_port() -> None:
    # High unassigned port should be closed
    assert is_port_in_use("127.0.0.1", 65530, timeout=0.2) is False


def test_get_compose_file_finds_project_compose() -> None:
    compose = get_compose_file()
    assert compose is not None
    assert compose.name == "docker-compose.yml"
    assert compose.exists()


def test_is_docker_available_returns_bool() -> None:
    result = is_docker_available()
    assert isinstance(result, bool)


def test_ensure_infrastructure_online_structure() -> None:
    status = ensure_infrastructure_online(auto_start_docker=False)
    assert "postgres" in status
    assert "redis" in status
    assert "status" in status
