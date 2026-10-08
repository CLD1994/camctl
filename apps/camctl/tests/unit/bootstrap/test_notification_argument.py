import pytest

from camctl.cli import CommandLineError, parse_command


def test_run_accepts_notification_descriptor():
    command = parse_command(["run", "--host-notification-fd", "9"])
    assert command.host_notification_fd == 9


@pytest.mark.parametrize("value", ["0", "1", "2", "-1", "1.0", "abc", "999999999999999999"])
def test_rejects_invalid_descriptor_syntax(value):
    with pytest.raises(CommandLineError):
        parse_command(["run", "--host-notification-fd", value])


@pytest.mark.parametrize("command", ["submit", "init", "describe"])
def test_only_run_accepts_notification_descriptor(command):
    args = [command, "--host-notification-fd", "9"]
    if command == "submit":
        args.append("plan.json")
    with pytest.raises(CommandLineError):
        parse_command(args)
