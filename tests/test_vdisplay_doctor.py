from ndi_broadcaster.vdisplay_doctor import (
    ProcessRecord,
    parse_ps_output,
)


def test_parse_ps_output_reads_pid_ppid_and_full_command():
    # Real `ps -Awwo pid=,ppid=,command=` output: right-aligned numeric
    # columns, then the full argument vector, which itself contains spaces.
    text = (
        "51006     1 uv run python -m ndi_broadcaster.launcher\n"
        " 4903     1 /repo/ndi_broadcaster/vdisplay_helper/vdisplay_helper 3840 2160 Some Name\n"
    )

    table = parse_ps_output(text)

    assert table[51006] == ProcessRecord(
        pid=51006, ppid=1, command="uv run python -m ndi_broadcaster.launcher"
    )
    assert table[4903].ppid == 1
    assert table[4903].command.endswith("vdisplay_helper 3840 2160 Some Name")


def test_parse_ps_output_skips_blank_and_malformed_lines():
    text = "\n   \n123 456\n789 1 real-command\n"

    table = parse_ps_output(text)

    assert list(table) == [789]


def test_parse_ps_output_keeps_commands_containing_many_spaces():
    text = "  42   1 python -c import time; time.sleep(6) # pad pad pad\n"

    table = parse_ps_output(text)

    assert table[42].command == "python -c import time; time.sleep(6) # pad pad pad"
