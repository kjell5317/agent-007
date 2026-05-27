from app.auth.middleware import _is_exempt_request


def test_close_task_post_is_auth_exempt() -> None:
    assert _is_exempt_request(
        "POST",
        "/tasks/123e4567-e89b-12d3-a456-426614174000/close",
    )


def test_close_task_exemption_requires_post_and_valid_uuid() -> None:
    assert not _is_exempt_request(
        "GET",
        "/tasks/123e4567-e89b-12d3-a456-426614174000/close",
    )
    assert not _is_exempt_request("POST", "/tasks/not-a-uuid/close")
    assert not _is_exempt_request(
        "POST",
        "/tasks/123e4567-e89b-12d3-a456-426614174000/reopen",
    )
