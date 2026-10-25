"""Raspberry Pi boot configuration utility safety and idempotence."""

from pathlib import Path

import pytest

from tools.setup_pi import SetupError, SetupOptions, configure_files, main


def files(
    tmp_path,
    config="",
    cmdline="console=serial0,115200 root=/dev/mmcblk0p2\n",
    module_load="i2c-dev\n",
):
    boot = tmp_path / "boot" / "firmware"
    boot.mkdir(parents=True)
    config_path = boot / "config.txt"
    cmdline_path = boot / "cmdline.txt"
    modules_load_dir = tmp_path / "etc" / "modules-load.d"
    modules_load_dir.mkdir(parents=True)
    modules_load_path = modules_load_dir / "i2c-dev.conf"
    config_path.write_text(config, encoding="utf-8")
    cmdline_path.write_text(cmdline, encoding="utf-8")
    if module_load is not None:
        modules_load_path.write_text(module_load, encoding="utf-8")
    return config_path, cmdline_path, modules_load_path


def model_file(tmp_path, model):
    path = tmp_path / "proc" / "device-tree" / "model"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(model.encode("utf-8") + b"\0")
    return path


def configure(paths, **changes):
    return configure_files(SetupOptions(**changes), *paths)


@pytest.mark.parametrize(
    "model",
    [
        "Raspberry Pi 3 Model B Plus Rev 1.3",
        "Raspberry Pi 4 Model B Rev 1.5",
        "Raspberry Pi Zero 2 W Rev 1.0",
        "Raspberry Pi Zero W Rev 1.1",
        "Raspberry Pi Model B Rev 2",
        "Raspberry Pi 2 Model B Rev 1.1",
    ],
)
def test_pre_pi5_defaults_to_software_i2c_bus_8(tmp_path, capsys, model):
    paths = files(tmp_path)
    assert main([], *paths, model_file(tmp_path, model)) == 0
    config = paths[0].read_text(encoding="utf-8")
    assert "dtparam=i2c_arm=off" in config
    assert (
        "dtoverlay=i2c-gpio,bus=8,i2c_gpio_sda=2,i2c_gpio_scl=3,"
        "i2c_gpio_delay_us=2"
    ) in config
    assert "dtparam=spi=on" in config
    assert config.count("core_freq_fixed=1") == 1
    assert "force_turbo" not in config
    assert "I2C device: /dev/i2c-8" in capsys.readouterr().out


def test_pi5_defaults_to_hardware_i2c_at_100khz(tmp_path, capsys):
    paths = files(tmp_path, "# Raspberry Pi boot settings\n")
    result = main(
        [], *paths, model_file(tmp_path, "Raspberry Pi 5 Model B Rev 1.0")
    )
    assert result == 0
    assert paths[0].read_text(encoding="utf-8") == (
        "# Raspberry Pi boot settings\n\n"
        "dtparam=i2c_arm=on\n"
        "dtparam=i2c_arm_baudrate=100000\n"
        "dtparam=spi=on\n"
    )
    assert paths[1].read_text(encoding="utf-8") == (
        "console=serial0,115200 root=/dev/mmcblk0p2 spidev.bufsiz=8192\n"
    )
    output = capsys.readouterr().out
    assert "I2C configuration: hardware GPIO2/3 at 100000 Hz" in output
    assert "I2C device: /dev/i2c-1" in output
    assert "SPI0: enabled" in output
    assert "spidev.bufsiz: 8192" in output
    assert "Reboot required: yes" in output


def test_unknown_model_requires_explicit_mode_without_modifying_files(tmp_path, capsys):
    paths = files(tmp_path)
    before = tuple(path.read_bytes() for path in paths)
    with pytest.raises(SystemExit) as exc:
        main([], *paths, model_file(tmp_path, "Future Board 1"))
    assert exc.value.code == 2
    assert "Unsupported or unknown" in capsys.readouterr().err
    assert tuple(path.read_bytes() for path in paths) == before


@pytest.mark.parametrize(
    "model,args,expected_device,expected_setting",
    [
        (
            "Raspberry Pi 5 Model B Rev 1.0",
            ["--i2c-mode", "sw", "--i2c-bus", "12", "--i2c-delay-us", "4"],
            "/dev/i2c-12",
            "bus=12,i2c_gpio_sda=2,i2c_gpio_scl=3,i2c_gpio_delay_us=4",
        ),
        (
            "Raspberry Pi 4 Model B Rev 1.5",
            ["--i2c-mode", "hw", "--i2c-speed", "400000"],
            "/dev/i2c-1",
            "dtparam=i2c_arm_baudrate=400000",
        ),
    ],
)
def test_cli_mode_overrides_model_default(
    tmp_path, capsys, model, args, expected_device, expected_setting
):
    paths = files(tmp_path)
    assert main(args, *paths, model_file(tmp_path, model)) == 0
    assert expected_setting in paths[0].read_text(encoding="utf-8")
    assert ("core_freq_fixed=1" in paths[0].read_text()) == ("Pi 4" in model)
    assert f"I2C device: {expected_device}" in capsys.readouterr().out


def test_explicit_mode_allows_unknown_or_missing_model_file(tmp_path):
    paths = files(tmp_path)
    missing_model = tmp_path / "missing-model"
    assert main(["--i2c-mode", "sw"], *paths, missing_model) == 0
    assert "bus=8" in paths[0].read_text(encoding="utf-8")
    assert "core_freq" not in paths[0].read_text()


@pytest.mark.parametrize("setting", ["core_freq=500", "core_freq_min=500", "core_freq_fixed=1"])
def test_core_clock_conflicts_fail_before_any_changes(tmp_path, setting):
    paths = files(tmp_path, f"# keep\n{setting}\n", module_load=None)
    before = [path.read_bytes() for path in paths[:2]]
    with pytest.raises(SetupError, match="line 2: " + setting):
        configure(paths, fix_core_clock=True)
    assert [path.read_bytes() for path in paths[:2]] == before
    assert not paths[2].exists()
    assert not list(tmp_path.rglob("*.fmheadend.bak*"))


def test_core_clock_overwrite_preserves_comments_and_is_idempotent(tmp_path):
    original = (
        "# core_freq=400\ncore_freq=500 # user note\ncore_freq_min=500\n"
        "core_freq_fixed=0\ncore_freq_fixed=1\n"
        "dtparam=audio=on,spi=off # combined\n"
        "[pi4]\ngpu_mem=64\n[none]\ncore_freq_min=200\n"
    )
    paths = files(tmp_path, original)
    first = configure(paths, fix_core_clock=True, overwrite_existing=True)
    result = paths[0].read_text()
    assert "# core_freq=400\n# user note\n" in result
    assert "dtparam=audio=on # combined" in result
    assert "[pi4]\ngpu_mem=64\n[none]\ncore_freq_min=200\n" in result
    assert result.count("core_freq_fixed=1") == 1
    assert "core_freq_fixed=0" not in result and "core_freq_min=500" not in result
    assert "\n[all]\n" in result
    assert first.backups[0].read_text() == original
    assert first.reboot_required
    second = configure(paths, fix_core_clock=True, overwrite_existing=True)
    assert not second.changed and not second.reboot_required
    assert paths[0].read_text() == result


@pytest.mark.parametrize("section", ["pi4", "pi5", "tryboot", "0x12345678"])
@pytest.mark.parametrize("setting", ["core_freq=500", "core_freq_min=500", "core_freq_fixed=1"])
def test_conditional_core_clock_is_rejected_without_changes(tmp_path, section, setting):
    paths = files(tmp_path, f"[{section}]\n{setting}\n")
    before = [path.read_bytes() for path in paths]
    with pytest.raises(SetupError, match="conditional.*line 2.*ambiguous"):
        configure(paths, fix_core_clock=True, overwrite_existing=True)
    assert [path.read_bytes() for path in paths] == before
    assert not list(tmp_path.rglob("*.fmheadend.bak*"))


def test_pi5_preserves_all_core_clock_settings_even_with_overwrite(tmp_path, capsys):
    original = "core_freq=910\ncore_freq_min=500\n[pi5]\ncore_freq_fixed=0\n"
    paths = files(tmp_path, original)
    model = model_file(tmp_path, "Raspberry Pi 5 Model B Rev 1.0")
    assert main(["--overwrite-existing"], *paths, model) == 0
    result = paths[0].read_text()
    assert result.startswith(original)
    assert "core_freq_fixed=1" not in result
    assert "Core clock: unchanged" in capsys.readouterr().out


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("config", [
    "",
    "core_freq=450 # keep\ncore_freq_min=300\ncore_freq_fixed=1\n",
    "[pi4]\ncore_freq=500\ncore_freq_min=250\ncore_freq_fixed=0\n",
])
def test_keep_core_clock_preserves_settings_even_with_overwrite(tmp_path, capsys, overwrite, config):
    paths = files(tmp_path, config)
    model = model_file(tmp_path, "Raspberry Pi 4 Model B Rev 1.5")
    args = ["--keep-core-clock"] + (["--overwrite-existing"] if overwrite else [])
    assert main(args, *paths, model) == 0
    result = paths[0].read_text()
    assert result.startswith(config)
    assert [line for line in result.splitlines() if "core_freq" in line] == [
        line for line in config.splitlines() if "core_freq" in line
    ]
    assert "dtparam=spi=on" in result
    assert "Core clock: unchanged (--keep-core-clock)" in capsys.readouterr().out
    assert main(["--keep-core-clock", "--overwrite-existing"], *paths, model) == 0
    assert paths[0].read_text() == result
    assert "Reboot required: no" in capsys.readouterr().out


def test_core_clock_plan_not_written_if_later_preflight_fails(tmp_path):
    paths = files(tmp_path, "core_freq_min=500\n", cmdline="root=x\nquiet\n")
    before = [path.read_bytes() for path in paths]
    with pytest.raises(SetupError, match="exactly one"):
        configure(paths, fix_core_clock=True, overwrite_existing=True)
    assert [path.read_bytes() for path in paths] == before


@pytest.mark.parametrize(
    "args,message",
    [
        (["--i2c-mode", "sw", "--i2c-speed", "25000"], "only in hardware"),
        (["--i2c-mode", "hw", "--i2c-bus", "8"], "only in software"),
        (["--i2c-mode", "hw", "--i2c-delay-us", "2"], "only in software"),
    ],
)
def test_cli_rejects_incompatible_mode_specific_options(
    tmp_path, capsys, args, message
):
    paths = files(tmp_path)
    before = tuple(path.read_bytes() for path in paths)
    with pytest.raises(SystemExit) as exc:
        main(args, *paths, model_file(tmp_path, "Raspberry Pi 4 Model B"))
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
    assert tuple(path.read_bytes() for path in paths) == before


def test_existing_entries_fail_with_file_line_and_entry_before_changes(tmp_path):
    paths = files(
        tmp_path,
        "dtparam=i2c_arm=on\ndtparam=spi=off\n",
        "root=x spidev.bufsiz=4096\n",
    )
    before = tuple(path.read_bytes() for path in paths)
    with pytest.raises(SetupError) as exc:
        configure(paths)
    message = str(exc.value)
    assert str(paths[0]) in message
    assert "line 1: dtparam=i2c_arm=on" in message
    assert "--overwrite-existing" in message
    assert tuple(path.read_bytes() for path in paths) == before
    assert not list(paths[0].parent.glob("*.fmheadend.bak*"))


def test_cmdline_conflict_does_not_apply_planned_config_change(tmp_path):
    paths = files(tmp_path, cmdline="root=x spidev.bufsiz=4096\n")
    before = tuple(path.read_bytes() for path in paths)
    with pytest.raises(SetupError) as exc:
        configure(paths)
    assert f"{paths[1]}: line 1: spidev.bufsiz=4096" in str(exc.value)
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
    assert config.count("dtoverlay=i2c-gpio") == 1
    assert config.count("dtparam=spi=") == 1
    assert paths[1].read_text(encoding="utf-8") == (
        "root=x quiet splash spidev.bufsiz=8192\n"
    )


def test_switching_hardware_to_software_removes_old_settings(tmp_path):
    paths = files(
        tmp_path,
        "dtparam=i2c_arm=on\ndtparam=i2c_arm_baudrate=100000\ndtparam=spi=on\n",
        "root=x spidev.bufsiz=8192\n",
    )
    result = configure(paths, overwrite_existing=True)
    config = paths[0].read_text(encoding="utf-8")
    assert "i2c_arm_baudrate" not in config
    assert "dtparam=i2c_arm=off" in config
    assert config.count("dtoverlay=i2c-gpio") == 1
    assert result.i2c_device == "/dev/i2c-8"


def test_switching_software_to_hardware_removes_gpio_overlay(tmp_path):
    paths = files(
        tmp_path,
        "dtparam=i2c_arm=off\n"
        "dtoverlay=i2c-gpio,bus=8,i2c_gpio_sda=2,i2c_gpio_scl=3,"
        "i2c_gpio_delay_us=2\n"
        "dtparam=spi=on\n",
        "root=x spidev.bufsiz=8192\n",
    )
    configure(paths, i2c_mode="hw", overwrite_existing=True)
    config = paths[0].read_text(encoding="utf-8")
    assert "dtoverlay=i2c-gpio" not in config
    assert "dtparam=i2c_arm=on" in config
    assert "dtparam=i2c_arm_baudrate=100000" in config


def test_software_bus_conflict_is_rejected_even_with_overwrite(tmp_path):
    paths = files(
        tmp_path,
        "dtoverlay=i2c-gpio,bus=8,i2c_gpio_sda=17,i2c_gpio_scl=27\n",
    )
    before = tuple(path.read_bytes() for path in paths)
    with pytest.raises(SetupError, match="bus 8 is already used"):
        configure(paths, overwrite_existing=True)
    assert tuple(path.read_bytes() for path in paths) == before


def test_unrelated_software_i2c_overlay_is_preserved(tmp_path):
    paths = files(
        tmp_path,
        "dtoverlay=i2c-gpio,bus=9,i2c_gpio_sda=17,i2c_gpio_scl=27\n",
    )
    configure(paths)
    config = paths[0].read_text(encoding="utf-8")
    assert "dtoverlay=i2c-gpio,bus=9,i2c_gpio_sda=17,i2c_gpio_scl=27" in config
    assert "dtoverlay=i2c-gpio,bus=8,i2c_gpio_sda=2,i2c_gpio_scl=3" in config


def test_repeated_overwrite_run_is_idempotent_and_has_no_duplicates(tmp_path):
    paths = files(tmp_path)
    first = configure(paths, overwrite_existing=True)
    first_config = paths[0].read_bytes()
    first_cmdline = paths[1].read_bytes()
    second = configure(paths, overwrite_existing=True)
    assert second.changed == ()
    assert not second.reboot_required
    assert paths[0].read_bytes() == first_config
    assert paths[1].read_bytes() == first_cmdline
    assert paths[0].read_text(encoding="utf-8").count("dtparam=spi=on") == 1
    assert paths[0].read_text(encoding="utf-8").count("dtoverlay=i2c-gpio") == 1
    assert paths[1].read_text(encoding="utf-8").count("spidev.bufsiz=8192") == 1
    assert len(first.backups) == 2


@pytest.mark.parametrize("i2c_mode", ["hw", "sw"])
def test_missing_module_load_configuration_is_created(tmp_path, i2c_mode):
    paths = files(tmp_path, module_load=None)
    result = configure(paths, i2c_mode=i2c_mode)
    assert paths[2].read_text(encoding="utf-8") == "i2c-dev\n"
    assert paths[2] in result.changed
    assert result.backups[result.changed.index(paths[2])] is None


def test_main_reports_created_module_load_configuration(tmp_path, capsys):
    paths = files(tmp_path, module_load=None)
    model = model_file(tmp_path, "Raspberry Pi 4 Model B Rev 1.5")
    assert main([], *paths, model) == 0
    assert f"Created {paths[2]}" in capsys.readouterr().out


def test_correct_module_load_configuration_is_left_unchanged(tmp_path):
    paths = files(tmp_path, module_load="# keep this comment\n\ni2c-dev\n")
    before = paths[2].read_bytes()
    result = configure(paths)
    assert paths[2].read_bytes() == before
    assert paths[2] not in result.changed


def test_conflicting_module_load_configuration_fails_before_changes(tmp_path):
    paths = files(tmp_path, module_load="i2c-bcm2835\n")
    before = tuple(path.read_bytes() for path in paths)
    with pytest.raises(SetupError, match="conflicting module-load configuration") as exc:
        configure(paths)
    assert f"{paths[2]}: line 1: i2c-bcm2835" in str(exc.value)
    assert tuple(path.read_bytes() for path in paths) == before
    assert not list(tmp_path.rglob("*.fmheadend.bak*"))


def test_overwrite_replaces_and_backs_up_conflicting_module_load_file(tmp_path):
    paths = files(tmp_path, module_load="i2c-bcm2835\n")
    result = configure(paths, overwrite_existing=True)
    assert paths[2].read_text(encoding="utf-8") == "i2c-dev\n"
    backup = result.backups[result.changed.index(paths[2])]
    assert backup is not None
    assert backup.read_text(encoding="utf-8") == "i2c-bcm2835\n"


def test_existing_correct_values_still_require_overwrite(tmp_path):
    paths = files(
        tmp_path,
        "dtparam=i2c_arm=off\n"
        "dtoverlay=i2c-gpio,bus=8,i2c_gpio_sda=2,i2c_gpio_scl=3,"
        "i2c_gpio_delay_us=2\n"
        "dtparam=spi=on\n",
        "root=x spidev.bufsiz=8192\n",
    )
    with pytest.raises(SetupError, match="overwrite-existing"):
        configure(paths)
    result = configure(paths, overwrite_existing=True)
    assert result.changed == ()


def test_keep_spi_bufsiz_leaves_cmdline_completely_unchanged(tmp_path):
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


def test_commented_settings_are_not_conflicts(tmp_path):
    paths = files(
        tmp_path,
        "# dtparam=i2c_arm=on\n# dtoverlay=i2c-gpio,bus=8\n# dtparam=spi=off\n",
        "root=x quiet\n",
    )
    configure(paths)
    assert "# dtparam=i2c_arm=on" in paths[0].read_text(encoding="utf-8")


def test_none_section_settings_are_inactive_and_preserved(tmp_path):
    paths = files(
        tmp_path,
        "[none]\n"
        "dtparam=i2c_arm=on,spi=off\n"
        "dtoverlay=i2c-gpio,bus=8,i2c_gpio_sda=17,i2c_gpio_scl=27\n",
    )
    configure(paths)
    config = paths[0].read_text(encoding="utf-8")
    assert "[none]\ndtparam=i2c_arm=on,spi=off" in config
    assert "[all]" in config
    assert "dtoverlay=i2c-gpio,bus=8,i2c_gpio_sda=2,i2c_gpio_scl=3" in config


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
    "options,message",
    [
        (SetupOptions(i2c_speed=0), "speed"),
        (SetupOptions(i2c_delay_us=0), "delay"),
        (SetupOptions(i2c_bus=256), "bus"),
    ],
)
def test_invalid_options_are_rejected_before_file_access(tmp_path, options, message):
    with pytest.raises(SetupError, match=message):
        configure_files(options, tmp_path / "missing-config", tmp_path / "missing-cmdline")


def test_cmdline_must_be_one_line_without_partial_changes(tmp_path):
    paths = files(tmp_path, cmdline="root=x\nquiet\n")
    before = tuple(path.read_bytes() for path in paths)
    with pytest.raises(SetupError, match="exactly one"):
        configure(paths)
    assert tuple(path.read_bytes() for path in paths) == before


def test_missing_boot_configuration_layout_is_rejected(tmp_path):
    config = tmp_path / "boot" / "firmware" / "config.txt"
    cmdline = config.with_name("cmdline.txt")
    with pytest.raises(SetupError, match="regular file"):
        configure_files(SetupOptions(), config, cmdline)
