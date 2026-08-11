from layout_server.log_format import log_format


def test_log_format_without_port_matches_original_format():
    assert log_format({}) == "%(asctime)s %(levelname)s %(name)s: %(message)s"


def test_log_format_with_port_adds_prefix():
    assert log_format({"LAYOUT_DRIVER_PORT": "8444"}) == (
        "%(asctime)s [:8444] %(levelname)s %(name)s: %(message)s"
    )
