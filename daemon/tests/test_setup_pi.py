"""Raspberry Pi boot configuration utility safety and idempotence."""

from pathlib import Path

import pytest

from scripts.setup_pi import SetupError, SetupOptions, configure_files, main


def files(tmp_path, config="", cmdline="console=serial0,115200 root=/dev/mmcblk0p2\n"):
    boot = tmp_path / "boot" / "firmware"
    boot.mkdir(parents=True)
    config_path = boot / "config.txt"
    cmdline_path = boot / "cmdline.txt"
    config_path.write_text(config, encoding="utf-8")
    cmdline_path.write_text(cmdline, encoding="utf-8")
    return config_path, cmdline_path


def configure(paths, **changes):
    return configure_files(SetupOptions(**changes), *paths)


def test_defaults_configure_hardware_i2c_spi_and_buffer(tmp_path):
    paths = files(tmp_path, "# Raspberry Pi boot settings\n")
    result = configure(paths)
    assert paths[0].read_text(encoding="utf-8") == (
        "# Raspberry Pi boot settings\n\n"
        "dtparam=i2c_arm=on,i2c_arm_baudrate=25000\n"
        "dtparam=spi=on\n"
    )
    assert paths[1].read_text(encoding="utf-8") == (
        "console=serial0,115200 root=/dev/mmcblk0p2 spidev.bufsiz=8192\n"
    )
    assert result.changed == paths
    assert result.i2c_device == "/dev/i2c-1"
    assert result.reboot_required
    assert all(path.is_file() for path in result.backups)
    assert result.backups[0].read_text(encoding="utf-8") == "# Raspberry Pi boot settings\n"
    assert result.backups[1].read_text(encoding="utf-8") == (
        "console=serial0,115200 root=/dev/mmcblk0p2\n"
    )


def test_conflicts_fail_before_any_file_or_backup_changes(tmp_path):
    paths = files(tmp_path, "dtparam=i2c_arm=on\ndtparam=spi=off\n", "root=x spidev.bufsiz=4096\n")
    before = tuple(path.read_bytes() for path in paths)
    with pytest.raises(SetupError, match="overwrite-existing"):
        configure(paths)
    assert tuple(path.read_bytes() for path in paths) == before
    assert not list(paths[0].parent.glob("*.fmheadend.bak*"))


def test_cmdline_conflict_does_not_apply_an_already_planned_config_change(tmp_path):
    paths = files(tmp_path, cmdline="root=x spidev.bufsiz=4096\n")
    before = tuple(path.read_bytes() for path in paths)
    with pytest.raises(SetupError, match="overwrite-existing"):
        configure(paths)
    assert tuple(path.read_bytes() for path in paths) == before
    assert not list(paths[0].parent.glob("*.fmheadend.bak*"))


def test_overwrite_preserves_combined_parameters_comments_and_sections(tmp_path):
    paths = files(
        tmp_path,
        "# keep\n"
        "dtparam=audio=on,i2c_arm=on,spi=off # combined\n"
        "dtparam=i2c_arm_baudrate=100000\n"
        "[pi4]\n"
        "dtoverlay=vc4-kms-v3d\n"
        "[all]\n"
        "gpu_mem=64\n",
        "root=x quiet spidev.bufsiz=4096 splash\n",
    )
    configure(paths, overwrite_existing=True)
    config = paths[0].read_text(encoding="utf-8")
    assert "dtparam=audio=on # combined" in config
    assert "[pi4]\ndtoverlay=vc4-kms-v3d\n[all]\ngpu_mem=64" in config
    assert config.count("i2c_arm=") == 1
    assert config.count("i2c_arm_baudrate=") == 1
    assert config.count("dtparam=spi=") == 1
    assert paths[1].read_text(encoding="utf-8") == (
        "root=x quiet splash spidev.bufsiz=8192\n"
    )


def test_software_i2c_requires_explicit_bus_and_uses_gpio2_3(tmp_path):
    paths = files(tmp_path)
    with pytest.raises(SetupError, match="i2c-bus"):
        configure(paths, i2c_mode="sw")
    result = configure(paths, i2c_mode="sw", i2c_bus=12, i2c_delay_us=4)
    config = paths[0].read_text(encoding="utf-8")
    assert "dtparam=i2c_arm=off" in config
    assert (
        "dtoverlay=i2c-gpio,bus=12,i2c_gpio_sda=2,i2c_gpio_scl=3,"
        "i2c_gpio_delay_us=4"
    ) in config
    assert result.i2c_device == "/dev/i2c-12"


def test_repeated_run_is_idempotent_and_creates_no_duplicates(tmp_path):
    paths = files(tmp_path)
    first = configure(paths)
    first_config = paths[0].read_bytes()
    first_cmdline = paths[1].read_bytes()
    second = configure(paths)
    assert second.changed == ()
    assert not second.reboot_required
    assert paths[0].read_bytes() == first_config
    assert paths[1].read_bytes() == first_cmdline
    assert paths[0].read_text(encoding="utf-8").count("dtparam=spi=on") == 1
    assert paths[1].read_text(encoding="utf-8").count("spidev.bufsiz=8192") == 1
    assert len(first.backups) == 2


def test_cli_reports_device_changes_and_reboot(tmp_path, capsys):
    paths = files(tmp_path)
    assert main([], *paths) == 0
    output = capsys.readouterr().out
    assert f"Updated {paths[0]}" in output
    assert f"Updated {paths[1]}" in output
    assert "I2C configuration: hardware GPIO2/3 at 25000 Hz" in output
    assert "SPI0: enabled" in output
    assert "spidev.bufsiz: 8192" in output
    assert "I2C device: /dev/i2c-1" in output
    assert "Reboot required: yes" in output


def test_keep_spi_bufsiz_leaves_cmdline_unchanged(tmp_path):
    paths = files(tmp_path, cmdline="root=x spidev.bufsiz=2048 quiet\n")
    before = paths[1].read_bytes()
    result = configure(paths, keep_spi_bufsiz=True)
    assert paths[1].read_bytes() == before
    assert result.changed == (paths[0],)


@pytest.mark.parametrize("section", ["pi4", "cm4", "pi5"])
def test_relevant_conditional_configuration_is_ambiguous(tmp_path, section):
    paths = files(tmp_path, f"[{section}]\ndtparam=spi=on\n")
    with pytest.raises(SetupError, match="ambiguous"):
        configure(paths, overwrite_existing=True)


def test_combined_exact_configuration_is_accepted_without_overwrite(tmp_path):
    paths = files(
        tmp_path,
        "dtparam=audio=on,i2c_arm=on,i2c_arm_baudrate=25000,spi=on\n",
        "root=x spidev.bufsiz=8192\n",
    )
    result = configure(paths)
    assert result.changed == ()
    assert not list(paths[0].parent.glob("*.fmheadend.bak*"))


def test_active_or_conditional_include_is_rejected(tmp_path):
    for config in ("include usercfg.txt\n", "[pi4]\ninclude pi4.txt\n"):
        paths = files(tmp_path / str(len(config)), config)
        with pytest.raises(SetupError, match="may contain relevant settings"):
            configure(paths, overwrite_existing=True)


def test_overwrite_collapses_duplicate_cmdline_settings(tmp_path):
    paths = files(
        tmp_path,
        cmdline="root=x spidev.bufsiz=4096 quiet spidev.bufsiz=8192\n",
    )
    configure(paths, overwrite_existing=True)
    cmdline = paths[1].read_text(encoding="utf-8")
    assert cmdline == "root=x quiet spidev.bufsiz=8192\n"
    assert cmdline.count("spidev.bufsiz=") == 1


@pytest.mark.parametrize(
    "options, message",
    [
        (SetupOptions(i2c_mode="hw", i2c_bus=8), "valid only"),
        (SetupOptions(i2c_speed=0), "speed"),
        (SetupOptions(i2c_delay_us=0), "delay"),
    ],
)
def test_invalid_options_are_rejected_before_file_access(tmp_path, options, message):
    with pytest.raises(SetupError, match=message):
        configure_files(options, tmp_path / "missing-config", tmp_path / "missing-cmdline")


def test_cmdline_must_be_one_line(tmp_path):
    paths = files(tmp_path, cmdline="root=x\nquiet\n")
    with pytest.raises(SetupError, match="exactly one"):
        configure(paths)


def test_missing_boot_configuration_layout_is_rejected(tmp_path):
    config = tmp_path / "boot" / "firmware" / "config.txt"
    cmdline = config.with_name("cmdline.txt")
    with pytest.raises(SetupError, match="regular file"):
        configure_files(SetupOptions(), config, cmdline)
